import py
import os, sys, subprocess, socket
import re, array, struct
from rpython.tool.udir import udir
from rpython.translator.interactive import Translation
from rpython.rlib.rarithmetic import LONG_BIT, intmask
from rpython.rlib import objectmodel, revdb
from rpython.rlib.debug import debug_print
from rpython.rtyper.annlowlevel import cast_gcref_to_instance
from rpython.rtyper.lltypesystem import lltype, llmemory

from rpython.translator.revdb.message import *
from rpython.translator.revdb.process import ReplayProcess


class RDB(object):
    def __init__(self, filename, expected_argv):
        with open(filename, 'rb') as f:
            header = f.readline()
            self.buffer = f.read()
        assert header == 'RevDB:\t' + '\t'.join(expected_argv) + '\n'
        #
        self.cur = 0
        x = self.next('c'); assert x == '\x00'
        x = self.next(); assert x == 0x00FF0001
        x = self.next(); assert x == 0
        x = self.next(); assert x == 0
        self.argc = self.next()
        self.argv = self.next()
        self.read_check_argv(expected_argv)

    def next(self, mode='P'):
        p = self.cur
        self.cur = p + struct.calcsize(mode)
        return struct.unpack_from(mode, self.buffer, p)[0]

    def read_check_argv(self, expected):
        assert self.argc == len(expected)
        for i in range(self.argc):
            self.next()    # this is from "p = argv[i]"
            s = []
            # first we determine the length of the "char *p"
            while True:
                c = self.next('c')
                if c == '\x00':
                    break
                s.append(c)
            # then we really read the "char *" and copy it into a rpy string
            # (that's why this time we don't read the final \0)
            for c1 in s:
                c2 = self.next('c')
                assert c2 == c1
            assert ''.join(s) == expected[i]

    def number_of_stop_points(self):
        return struct.unpack_from("q", self.buffer, len(self.buffer) - 8)[0]

    def done(self):
        return self.cur == len(self.buffer)


def compile(self, entry_point, argtypes, backendopt=True,
            withsmallfuncsets=None):
    t = Translation(entry_point, None, gc="boehm")
    self.t = t
    t.config.translation.reverse_debugger = True
    t.config.translation.lldebug0 = True
    if withsmallfuncsets is not None:
        t.config.translation.withsmallfuncsets = withsmallfuncsets
    if not backendopt:
        t.disable(["backendopt_lltype"])
    t.annotate()
    t.rtype()
    if t.backendopt:
        t.backendopt()
    self.exename = t.compile_c()
    self.rdbname = os.path.join(os.path.dirname(str(self.exename)),
                                'log.rdb')

def run(self, *argv):
    env = os.environ.copy()
    env['PYPYRDB'] = self.rdbname
    t = self.t
    stdout, stderr = t.driver.cbuilder.cmdexec(' '.join(argv), env=env,
                                               expect_crash=9)
    print >> sys.stderr, stderr
    return stdout

def fetch_rdb(self, expected_argv):
    return RDB(self.rdbname, map(str, expected_argv))


class BaseRecordingTests(object):
    compile = compile
    run = run
    fetch_rdb = fetch_rdb


class TestRecording(BaseRecordingTests):

    def test_simple(self):
        def main(argv):
            print argv[1:]
            return 9
        self.compile(main, [], backendopt=False)
        assert self.run('abc d') == '[abc, d]\n'
        rdb = self.fetch_rdb([self.exename, 'abc', 'd'])
        # write() call
        x = rdb.next(); assert x == len('[abc, d]\n')
        x = rdb.next('i'); assert x == 0      # errno
        x = rdb.next('q'); assert x == 0      # number of stop points
        # that's all we should get from this simple example
        assert rdb.done()

    def test_identityhash(self):
        def main(argv):
            print [objectmodel.compute_identity_hash(argv),
                   objectmodel.compute_identity_hash(argv),
                   objectmodel.compute_identity_hash(argv)]
            return 9
        self.compile(main, [], backendopt=False)
        out = self.run('Xx')
        match = re.match(r'\[(-?\d+), \1, \1]\n', out)
        assert match
        hash_value = int(match.group(1))
        rdb = self.fetch_rdb([self.exename, 'Xx'])
        # compute_identity_hash() call, but only the first one
        x = rdb.next(); assert intmask(x) == intmask(hash_value)
        # write() call
        x = rdb.next(); assert x == len(out)
        x = rdb.next('i'); assert x == 0      # errno
        # done
        x = rdb.next('q'); assert x == 0      # number of stop points
        assert rdb.done()

    def test_dont_record_vtable_reads(self):
        class A(object):
            x = 42
        class B(A):
            x = 43
        lst = [A(), B()]
        def main(argv):
            print lst[len(argv) & 1].x
            return 9
        self.compile(main, [], backendopt=False)
        out = self.run('Xx')
        assert out == '42\n'
        rdb = self.fetch_rdb([self.exename, 'Xx'])
        # write() call (it used to be the case that vtable reads where
        # recorded too; the single byte fetched from the vtable from
        # the '.x' in main() would appear here)
        x = rdb.next(); assert x == len(out)
        x = rdb.next('i'); assert x == 0      # errno
        # done
        x = rdb.next('q'); assert x == 0      # number of stop points
        assert rdb.done()

    def test_dont_record_pbc_reads(self):
        class MyPBC:
            def _freeze_(self):
                return True
        pbc1 = MyPBC(); pbc1.x = 41
        pbc2 = MyPBC(); pbc2.x = 42
        lst = [pbc1, pbc2]
        def main(argv):
            print lst[len(argv) & 1].x
            return 9
        self.compile(main, [], backendopt=False)
        out = self.run('Xx')
        assert out == '41\n'
        rdb = self.fetch_rdb([self.exename, 'Xx'])
        # write() call
        x = rdb.next(); assert x == len(out)
        x = rdb.next('i'); assert x == 0      # errno
        # done
        x = rdb.next('q'); assert x == 0      # number of stop points
        assert rdb.done()

    @py.test.mark.parametrize('limit', [3, 5])
    def test_dont_record_small_funcset_conversions(self, limit):
        def f1():
            return 111
        def f2():
            return 222
        def f3():
            return 333
        def g(n):
            if n & 1:
                return f1
            else:
                return f2
        def main(argv):
            x = g(len(argv))    # can be f1 or f2
            if len(argv) > 5:
                x = f3  # now can be f1 or f2 or f3
            print x()
            return 9
        self.compile(main, [], backendopt=False, withsmallfuncsets=limit)
        for input, expected_output in [
                ('2 3', '111\n'),
                ('2 3 4', '222\n'),
                ('2 3 4 5 6 7', '333\n'),
                ]:
            out = self.run(input)
            assert out == expected_output
            rdb = self.fetch_rdb([self.exename] + input.split())
            # write() call
            x = rdb.next(); assert x == len(out)
            x = rdb.next('i'); assert x == 0      # errno
            x = rdb.next('q'); assert x == 0      # number of stop points
            assert rdb.done()


class InteractiveTests(object):

    def replay(self, **kwds):
        s1, s2 = socket.socketpair()
        subproc = subprocess.Popen(
            [str(self.exename), '--revdb-replay', str(self.rdbname),
             str(s2.fileno())], **kwds)
        s2.close()
        self.subproc = subproc
        child = ReplayProcess(subproc.pid, s1)
        child.expect(ANSWER_INIT, INIT_VERSION_NUMBER,
                     self.expected_stop_points)
        child.expect(ANSWER_READY, 1, Ellipsis)
        return child


class TestSimpleInterpreter(InteractiveTests):
    expected_stop_points = 3

    def setup_class(cls):
        def main(argv):
            lst = [argv[0], 'prebuilt']
            for op in argv[1:]:
                revdb.stop_point()
                print op
                lst.append(op + '??')   # create a new string here
            for x in lst:
                print revdb.get_unique_id(x)
            return 9
        compile(cls, main, [], backendopt=False)
        assert run(cls, 'abc d ef') == ('abc\nd\nef\n'
                                        '3\n0\n12\n15\n17\n')
        rdb = fetch_rdb(cls, [cls.exename, 'abc', 'd', 'ef'])
        assert rdb.number_of_stop_points() == 3

    def test_go(self):
        child = self.replay()
        child.send(Message(CMD_FORWARD, 2))
        child.expect(ANSWER_READY, 3, Ellipsis)
        child.send(Message(CMD_FORWARD, 2))
        child.expect(ANSWER_AT_END)

    def test_quit(self):
        child = self.replay()
        child.send(Message(CMD_QUIT))
        assert self.subproc.wait() == 0

    def test_fork(self):
        child = self.replay()
        child2 = child.clone()
        child.send(Message(CMD_FORWARD, 2))
        child.expect(ANSWER_READY, 3, Ellipsis)
        child2.send(Message(CMD_FORWARD, 1))
        child2.expect(ANSWER_READY, 2, Ellipsis)
        #
        child.close()
        child2.close()


class TestDebugCommands(InteractiveTests):
    expected_stop_points = 3

    def setup_class(cls):
        #
        class Stuff:
            pass
        #
        def g(cmdline):
            if len(cmdline) > 5:
                raise ValueError
        g._dont_inline_ = True
        #
        def went_fw():
            revdb.send_answer(120, revdb.current_time())
            if revdb.current_time() != revdb.total_time():
                revdb.go_forward(1, went_fw)
        #
        def _nothing(arg):
            pass
        #
        def callback_track_obj(gcref):
            revdb.send_output("callback_track_obj\n")
            dbstate.gcref = gcref
        #
        def blip(cmd, extra):
            debug_print('<<<', cmd.c_cmd, cmd.c_arg1,
                               cmd.c_arg2, cmd.c_arg3, extra, '>>>')
            if extra == 'oops':
                for i in range(1000):
                    print 42     # I/O not permitted
            if extra == 'raise-and-catch':
                try:
                    g(extra)
                except ValueError:
                    pass
            if extra == 'crash':
                raise ValueError
            if extra == 'get-value':
                revdb.send_answer(100, revdb.current_time(),
                                       revdb.total_time())
            ## if extra == 'go-fw':
            ##     revdb.go_forward(1, went_fw)
            ## if cmdline == 'set-break-after-0':
            ##     dbstate.break_after = 0
            ## if cmdline == 'print-id':
            ##     revdb.send_output('obj.x=%d %d %d\n' % (
            ##         dbstate.stuff.x,
            ##         revdb.get_unique_id(dbstate.stuff),
            ##         revdb.currently_created_objects()))
            ## if cmdline.startswith('track-object '):
            ##     uid = int(cmdline[len('track-object '):])
            ##     dbstate.gcref = lltype.nullptr(llmemory.GCREF.TO)
            ##     revdb.track_object(uid, callback_track_obj)
            ## if cmdline == 'get-tracked-object':
            ##     if dbstate.gcref:
            ##         revdb.send_output('got obj.x=%d\n' % (
            ##             cast_gcref_to_instance(Stuff, dbstate.gcref).x,))
            ##     else:
            ##         revdb.send_output('none\n')
            ## if cmdline == 'first-created-uid':
            ##     revdb.send_output('first-created-uid=%d\n' % (
            ##         revdb.first_created_object_uid(),))
            revdb.send_answer(42, cmd.c_cmd, -43, -44, extra)
        lambda_blip = lambda: blip
        #
        class DBState:
            pass
        dbstate = DBState()
        #
        def main(argv):
            revdb.register_debug_command(1, lambda_blip)
            for i, op in enumerate(argv[1:]):
                dbstate.stuff = Stuff()
                dbstate.stuff.x = i + 1000
                revdb.stop_point()
                print op
            return 9
        compile(cls, main, [], backendopt=False)
        assert run(cls, 'abc d ef') == 'abc\nd\nef\n'

    def test_run_blip(self):
        child = self.replay()
        child.send(Message(1, extra='foo'))
        child.expect(42, 1, -43, -44, 'foo')

    def test_io_not_permitted(self):
        child = self.replay(stderr=subprocess.PIPE)
        child.send(Message(1, extra='oops'))
        child.close()
        err = self.subproc.stderr.read()
        assert err.endswith(': Attempted to do I/O or access raw memory\n')

    def test_interaction_with_forward(self):
        child = self.replay()
        child.send(Message(CMD_FORWARD, 50))
        child.expect(ANSWER_AT_END)

    def test_raise_and_catch(self):
        child = self.replay()
        child.send(Message(1, extra='raise-and-catch'))
        child.expect(42, 1, -43, -44, 'raise-and-catch')

    def test_crash(self):
        child = self.replay(stderr=subprocess.PIPE)
        child.send(Message(1, extra='crash'))
        child.close()
        err = self.subproc.stderr.read()
        assert err.endswith('Command crashed with ValueError\n')

    def test_get_value(self):
        child = self.replay()
        child.send(Message(1, extra='get-value'))
        child.expect(100, 1, 3)

    ## def test_go_fw(self):
    ##     child = self.replay()
    ##     child.send(Message(1, extra='go-fw'))
    ##     child.expect(42, 1, -43, -44, 'go-fw')
    ##     child.expect(120, 2)
    ##     child.expect(120, 3)
    ##     child.send(Message(CMD_FORWARD, 0))
    ##     child.expect(ANSWER_READY, 3, Ellipsis)
