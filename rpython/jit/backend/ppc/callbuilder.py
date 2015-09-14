from rpython.jit.backend.ppc.arch import IS_PPC_64, WORD, PARAM_SAVE_AREA_OFFSET
from rpython.jit.backend.ppc.arch import THREADLOCAL_ADDR_OFFSET
import rpython.jit.backend.ppc.register as r
from rpython.jit.metainterp.history import INT, FLOAT
from rpython.jit.backend.llsupport.callbuilder import AbstractCallBuilder
from rpython.jit.backend.ppc.jump import remap_frame_layout
from rpython.rlib.objectmodel import we_are_translated
from rpython.jit.backend.llsupport import llerrno
from rpython.rtyper.lltypesystem import rffi


def follow_jump(addr):
    # xxx implement me
    return addr


class CallBuilder(AbstractCallBuilder):
    GPR_ARGS = [r.r3, r.r4, r.r5, r.r6, r.r7, r.r8, r.r9, r.r10]
    FPR_ARGS = r.MANAGED_FP_REGS
    assert FPR_ARGS == [r.f1, r.f2, r.f3, r.f4, r.f5, r.f6, r.f7,
                        r.f8, r.f9, r.f10, r.f11, r.f12, r.f13]
    RSHADOWPTR  = r.RCS1
    RFASTGILPTR = r.RCS2
    RSHADOWOLD  = r.RCS3

    def __init__(self, assembler, fnloc, arglocs, resloc):
        AbstractCallBuilder.__init__(self, assembler, fnloc, arglocs,
                                     resloc, restype=INT, ressize=None)

    def prepare_arguments(self):
        assert IS_PPC_64
        self.subtracted_to_sp = 0

        # Prepare arguments.  Note that this follows the convention where
        # a prototype is in scope, and doesn't take "..." arguments.  If
        # you were to call a C function with a "..." argument with cffi,
        # it would not go there but instead via libffi.  If you pretend
        # instead that it takes fixed arguments, then it would arrive here
        # but the convention is bogus for floating-point arguments.  (And,
        # to add to the mess, at least CPython's ctypes cannot be used
        # to call a "..." function with floating-point arguments.  As I
        # guess that it's a problem with libffi, it means PyPy inherits
        # the same problem.)
        arglocs = self.arglocs
        num_args = len(arglocs)

        non_float_locs = []
        non_float_regs = []
        float_locs = []
        for i in range(min(num_args, 8)):
            if arglocs[i].type != FLOAT:
                non_float_locs.append(arglocs[i])
                non_float_regs.append(self.GPR_ARGS[i])
            else:
                float_locs.append(arglocs[i])
        # now 'non_float_locs' and 'float_locs' together contain the
        # locations of the first 8 arguments

        if num_args > 8:
            # We need to make a larger PPC stack frame, as shown on the
            # picture in arch.py.  It needs to be 48 bytes + 8 * num_args.
            # The new SP back chain location should point to the top of
            # the whole stack frame, i.e. jumping over both the existing
            # fixed-sise part and the new variable-sized part.
            base = PARAM_SAVE_AREA_OFFSET
            varsize = base + 8 * num_args
            varsize = (varsize + 15) & ~15    # align
            self.mc.load(r.SCRATCH2.value, r.SP.value, 0)    # SP back chain
            self.mc.store_update(r.SCRATCH2.value, r.SP.value, -varsize)
            self.subtracted_to_sp = varsize

            # In this variable-sized part, only the arguments from the 8th
            # one need to be written, starting at SP + 112
            for n in range(8, num_args):
                loc = arglocs[n]
                if loc.type != FLOAT:
                    # after the 8th argument, a non-float location is
                    # always stored in the stack
                    if loc.is_reg():
                        src = loc
                    else:
                        src = r.r2
                        self.asm.regalloc_mov(loc, src)
                    self.mc.std(src.value, r.SP.value, base + 8 * n)
                else:
                    # the first 13 floating-point arguments are all passed
                    # in the registers f1 to f13, independently on their
                    # index in the complete list of arguments
                    if len(float_locs) < len(self.FPR_ARGS):
                        float_locs.append(loc)
                    else:
                        if loc.is_fp_reg():
                            src = loc
                        else:
                            src = r.FP_SCRATCH
                            self.asm.regalloc_mov(loc, src)
                        self.mc.stfd(src.value, r.SP.value, base + 8 * n)

        # We must also copy fnloc into FNREG
        non_float_locs.append(self.fnloc)
        non_float_regs.append(self.mc.RAW_CALL_REG)     # r2 or r12

        if float_locs:
            assert len(float_locs) <= len(self.FPR_ARGS)
            remap_frame_layout(self.asm, float_locs,
                               self.FPR_ARGS[:len(float_locs)],
                               r.FP_SCRATCH)

        remap_frame_layout(self.asm, non_float_locs, non_float_regs,
                           r.SCRATCH)


    def push_gcmap(self):
        pass  # XXX

    def pop_gcmap(self):
        pass  # XXX

    def emit_raw_call(self):
        self.mc.raw_call()

    def restore_stack_pointer(self):
        if self.subtracted_to_sp != 0:
            self.mc.addi(r.SP.value, r.SP.value, self.subtracted_to_sp)

    def load_result(self):
        assert (self.resloc is None or
                self.resloc is r.r3 or
                self.resloc is r.f1)


    def call_releasegil_addr_and_move_real_arguments(self, fastgil):
        assert self.is_call_release_gil
        RSHADOWPTR  = self.RSHADOWPTR
        RFASTGILPTR = self.RFASTGILPTR
        RSHADOWOLD  = self.RSHADOWOLD
        #
        # Save this thread's shadowstack pointer into r29, for later comparison
        gcrootmap = self.asm.cpu.gc_ll_descr.gcrootmap
        if gcrootmap:
            rst = gcrootmap.get_root_stack_top_addr()
            self.mc.load_imm(RSHADOWPTR, rst)
            self.mc.load(RSHADOWOLD.value, RSHADOWPTR.value, 0)
        #
        # change 'rpy_fastgil' to 0 (it should be non-zero right now)
        self.mc.load_imm(RFASTGILPTR, fastgil)
        self.mc.li(r.r0.value, 0)
        self.mc.lwsync()
        self.mc.std(r.r0.value, RFASTGILPTR.value, 0)
        #
        if not we_are_translated():        # for testing: we should not access
            self.mc.addi(r.SPP.value, r.SPP.value, 1)           # r31 any more


    def move_real_result_and_call_reacqgil_addr(self, fastgil):
        from rpython.jit.backend.ppc.codebuilder import OverwritingBuilder

        # try to reacquire the lock.  The following registers are still
        # valid from before the call:
        RSHADOWPTR  = self.RSHADOWPTR     # r30: &root_stack_top
        RFASTGILPTR = self.RFASTGILPTR    # r29: &fastgil
        RSHADOWOLD  = self.RSHADOWOLD     # r28: previous val of root_stack_top

        # Equivalent of 'r10 = __sync_lock_test_and_set(&rpy_fastgil, 1);'
        self.mc.li(r.r9.value, 1)
        retry_label = self.mc.currpos()
        self.mc.ldarx(r.r10.value, 0, RFASTGILPTR.value)  # load the lock value
        self.mc.stdcxx(r.r9.value, 0, RFASTGILPTR.value)  # try to claim lock
        self.mc.bc(6, 2, retry_label - self.mc.currpos()) # retry if failed
        self.mc.isync()

        self.mc.cmpdi(0, r.r10.value, 0)
        b1_location = self.mc.currpos()
        self.mc.trap()       # patched with a BEQ: jump if r10 is zero

        if self.asm.cpu.gc_ll_descr.gcrootmap:
            # When doing a call_release_gil with shadowstack, there
            # is the risk that the 'rpy_fastgil' was free but the
            # current shadowstack can be the one of a different
            # thread.  So here we check if the shadowstack pointer
            # is still the same as before we released the GIL (saved
            # in 'r7'), and if not, we fall back to 'reacqgil_addr'.
            XXXXXXXXXXXXXXXXXXX
            self.mc.LDR_ri(r.ip.value, r.r5.value, cond=c.EQ)
            self.mc.CMP_rr(r.ip.value, r.r7.value, cond=c.EQ)
            b1_location = self.mc.currpos()
            self.mc.BKPT()                       # BEQ below
            # there are two cases here: either EQ was false from
            # the beginning, or EQ was true at first but the CMP
            # made it false.  In the second case we need to
            # release the fastgil here.  We know which case it is
            # by checking again r3.
            self.mc.CMP_ri(r.r3.value, 0)
            self.mc.STR_ri(r.r3.value, r.r6.value, cond=c.EQ)
        #
        # save the result we just got
        RSAVEDRES = RFASTGILPTR     # can reuse this reg here
        reg = self.resloc
        if reg is not None:
            if reg.is_core_reg():
                self.mc.mr(RSAVEDRES.value, reg.value)
            elif reg.is_fp_reg():
                self.mc.stfd(reg.value, r.SP.value,
                             PARAM_SAVE_AREA_OFFSET + 7 * WORD)
        self.mc.load_imm(self.mc.RAW_CALL_REG, self.asm.reacqgil_addr)
        self.mc.raw_call()
        if reg is not None:
            if reg.is_core_reg():
                self.mc.mr(reg.value, RSAVEDRES.value)
            elif reg.is_fp_reg():
                self.mc.lfd(reg.value, r.SP.value,
                            PARAM_SAVE_AREA_OFFSET + 7 * WORD)

        # replace b1_location with BEQ(here)
        jmp_target = self.mc.currpos()
        pmc = OverwritingBuilder(self.mc, b1_location, 1)
        pmc.beq(jmp_target - b1_location)
        pmc.overwrite()

        if not we_are_translated():        # for testing: now we can access
            self.mc.addi(r.SPP.value, r.SPP.value, -1)          # r31 again


    def write_real_errno(self, save_err):
        if save_err & rffi.RFFI_READSAVED_ERRNO:
            # Just before a call, read '*_errno' and write it into the
            # real 'errno'.  A lot of registers are free here, notably
            # r11 and r0.
            if save_err & rffi.RFFI_ALT_ERRNO:
                rpy_errno = llerrno.get_alt_errno_offset(self.asm.cpu)
            else:
                rpy_errno = llerrno.get_rpy_errno_offset(self.asm.cpu)
            p_errno = llerrno.get_p_errno_offset(self.asm.cpu)
            self.mc.ld(r.r11.value, r.SP.value,
                       THREADLOCAL_ADDR_OFFSET + self.subtracted_to_sp)
            self.mc.lwz(r.r0.value, r.r11.value, rpy_errno)
            self.mc.ld(r.r11.value, r.r11.value, p_errno)
            self.mc.stw(r.r0.value, r.r11.value, 0)
        elif save_err & rffi.RFFI_ZERO_ERRNO_BEFORE:
            # Same, but write zero.
            p_errno = llerrno.get_p_errno_offset(self.asm.cpu)
            self.mc.ld(r.r11.value, r.SP.value,
                       THREADLOCAL_ADDR_OFFSET + self.subtracted_to_sp)
            self.mc.ld(r.r11.value, r.r11.value, p_errno)
            self.mc.li(r.r0.value, 0)
            self.mc.stw(r.r0.value, r.r11.value, 0)

    def read_real_errno(self, save_err):
        if save_err & rffi.RFFI_SAVE_ERRNO:
            # Just after a call, read the real 'errno' and save a copy of
            # it inside our thread-local '*_errno'.  Registers r4-r10
            # never contain anything after the call.
            if save_err & rffi.RFFI_ALT_ERRNO:
                rpy_errno = llerrno.get_alt_errno_offset(self.asm.cpu)
            else:
                rpy_errno = llerrno.get_rpy_errno_offset(self.asm.cpu)
            p_errno = llerrno.get_p_errno_offset(self.asm.cpu)
            self.mc.ld(r.r9.value, r.SP.value, THREADLOCAL_ADDR_OFFSET)
            self.mc.ld(r.r10.value, r.r9.value, p_errno)
            self.mc.lwz(r.r10.value, r.r10.value, 0)
            self.mc.stw(r.r10.value, r.r9.value, rpy_errno)
