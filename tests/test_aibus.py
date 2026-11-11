import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('aibus', str(ROOT / 'aibus'))
spec = importlib.util.spec_from_loader('aibus', loader)
aibus = importlib.util.module_from_spec(spec)
loader.exec_module(aibus)


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = aibus.main(list(argv))
    return code, out.getvalue()


def ls():
    code, out = run('ls', '--json')
    assert code == 0
    return {(r['source'], r['id']): r for r in json.loads(out)['items']}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name) / 'bus'
        os.environ['AIBUS_HOME'] = str(self.home)
        self.addCleanup(os.environ.pop, 'AIBUS_HOME', None)


class PutAndDone(Base):
    def test_put_creates_and_merges(self):
        run('put', 'xverify', 'run-1', '--title', 'check', '--ai', 'codex', '--pid', str(os.getpid()))
        run('put', 'xverify', 'run-1', '--progress', '2/4', '--now', 'step two')
        row = ls()[('xverify', 'run-1')]
        self.assertEqual(row['title'], 'check')
        self.assertEqual(row['ai'], 'codex')
        self.assertEqual(row['progress'], [2, 4])
        self.assertEqual(row['now'], 'step two')
        self.assertEqual(row['status'], 'running')
        self.assertLessEqual(row['started'], row['updated'])

    def test_done_sets_state_from_exit_code_and_expires(self):
        run('put', 'job', 'a')
        run('done', 'job', 'a', '--exit-code', '2', '--link', 'report=/tmp/r.md')
        row = ls()[('job', 'a')]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['links'], {'report': '/tmp/r.md'})
        run('done', 'job', 'a', '--exit-code', '0')
        self.assertEqual(ls()[('job', 'a')]['status'], 'finished')

    def test_done_records_final_progress(self):
        run('put', 'job', 'a', '--progress', '3/5')
        run('done', 'job', 'a', '--progress', '5/5', '--now', 'all steps done')
        row = ls()[('job', 'a')]
        self.assertEqual(row['progress'], [5, 5])
        self.assertEqual(row['now'], 'all steps done')
        self.assertEqual(row['status'], 'finished')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run('done', 'job', 'a', '--progress', '6/5')

    def test_expired_entries_hide_unless_all(self):
        run('put', 'job', 'a')
        run('done', 'job', 'a', '--keep', '0')
        time.sleep(0.01)
        self.assertNotIn(('job', 'a'), ls())
        code, out = run('ls', '--json', '--all')
        self.assertEqual([r['id'] for r in json.loads(out)['items']], ['a'])

    def test_files_and_folders_are_private(self):
        run('put', 'job', 'a')
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.home / 'job').stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.home / 'job' / 'a.json').stat().st_mode), 0o600)

    def test_text_is_clipped_to_one_line(self):
        run('put', 'job', 'a', '--now', 'line one\nline two ' + 'x' * 500)
        now = ls()[('job', 'a')]['now']
        self.assertNotIn('\n', now)
        self.assertLessEqual(len(now), aibus.TEXT_LIMIT)


class Judging(Base):
    def test_dead_pid_is_stopped(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        run('put', 'mcp', 'c1', '--pid', str(child.pid))
        self.assertEqual(ls()[('mcp', 'c1')]['status'], 'stopped')

    def test_finished_entry_ignores_dead_pid(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        run('put', 'mcp', 'c1', '--pid', str(child.pid))
        run('done', 'mcp', 'c1')
        self.assertEqual(ls()[('mcp', 'c1')]['status'], 'finished')

    def test_quiet_interval_job_is_late(self):
        run('put', 'mac', 'slow-log', '--interval', '300', '--state', 'ok')
        path = self.home / 'mac' / 'slow-log.json'
        data = json.loads(path.read_text())
        data['updated'] -= 300 * 2 + aibus.GRACE + 1
        path.write_text(json.dumps(data))
        self.assertEqual(ls()[('mac', 'slow-log')]['status'], 'late')

    def test_on_time_interval_job_keeps_its_state(self):
        run('put', 'mac', 'slow-log', '--interval', '300', '--state', 'warn')
        self.assertEqual(ls()[('mac', 'slow-log')]['status'], 'warn')


class Safety(Base):
    def test_names_cannot_leave_the_folder(self):
        for bad in ('../x', 'a/b', '.hidden', '', 'x' * 129):
            with self.assertRaises(SystemExit):
                run('put', bad, 'a')
            with self.assertRaises(SystemExit):
                run('put', 'job', bad)

    def test_bad_progress_and_link_are_refused(self):
        for bad in ('3/2', '1/0', 'two'):
            with self.assertRaises(SystemExit):
                run('put', 'job', 'a', '--progress', bad)
        with self.assertRaises(SystemExit):
            run('put', 'job', 'a', '--link', 'no-equals')

    def test_unreadable_file_is_a_warning_not_a_crash(self):
        run('put', 'job', 'a')
        (self.home / 'job' / 'broken.json').write_text('{not json')
        code, out = run('ls', '--json')
        data = json.loads(out)
        self.assertEqual([r['id'] for r in data['items']], ['a'])
        self.assertEqual(len(data['warnings']), 1)

    def test_symlinked_entry_is_skipped(self):
        run('put', 'job', 'a')
        target = Path(self.dir.name) / 'elsewhere.json'
        target.write_text(json.dumps({'source': 'job', 'id': 'evil'}))
        (self.home / 'job' / 'evil.json').symlink_to(target)
        self.assertNotIn(('job', 'evil'), ls())

    def test_symlinked_home_is_refused(self):
        real = Path(self.dir.name) / 'real'
        real.mkdir()
        self.home.symlink_to(real)
        with self.assertRaises(SystemExit):
            run('put', 'job', 'a')


class Cleanup(Base):
    def test_rm_and_gc(self):
        run('put', 'job', 'keep')
        run('put', 'job', 'old')
        run('done', 'job', 'old', '--keep', '0')
        path = self.home / 'job' / 'old.json'
        data = json.loads(path.read_text())
        data['expires'] -= 8 * 86400
        path.write_text(json.dumps(data))
        code, out = run('gc', '--days', '7')
        self.assertEqual(out.strip(), 'removed 1')
        self.assertFalse(path.exists())
        run('rm', 'job', 'keep')
        self.assertEqual(ls(), {})
        run('rm', 'job', 'missing')

    def test_concurrent_puts_keep_every_field(self):
        script = (f'import sys; sys.argv=["aibus"]; '
                  f'import importlib.machinery as m; '
                  f'l=m.SourceFileLoader("aibus", {str(ROOT / "aibus")!r}); mod=l.load_module(); '
                  f'mod.main(["put", "job", "a", "--link", sys.stdin.read().strip()])')
        procs = [subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                                  env=dict(os.environ)) for _ in range(8)]
        for n, proc in enumerate(procs):
            proc.communicate(f'k{n}=/v{n}'.encode())
        self.assertEqual(len(ls()[('job', 'a')]['links']), 8)


class ReviewFindings(Base):
    """Cases from the first cross-verification."""

    def write_raw(self, source, item, text):
        run('put', source, 'seed')
        path = self.home / source / f'{item}.json'
        path.write_text(text)
        return path

    def test_1_gc_rechecks_under_the_lock(self):
        run('put', 'mac', 'x', '--interval', '10')
        path = self.home / 'mac' / 'x.json'
        data = json.loads(path.read_text())
        data['updated'] -= 9 * 86400
        path.write_text(json.dumps(data))
        real_locked = aibus.locked

        @contextlib.contextmanager
        def refreshed_while_waiting(folder):
            fresh = json.loads(path.read_text())
            fresh['updated'] = time.time()
            path.write_text(json.dumps(fresh))
            with real_locked(folder):
                yield
        aibus.locked = refreshed_while_waiting
        try:
            code, out = run('gc', '--days', '7')
        finally:
            aibus.locked = real_locked
        self.assertTrue(path.exists())
        self.assertEqual(out.strip(), 'removed 0')

    def test_2_ls_refuses_a_symlinked_home(self):
        real = Path(self.dir.name) / 'real'
        (real / 'job').mkdir(parents=True)
        (real / 'job' / 'a.json').write_text(json.dumps({'source': 'job', 'id': 'a'}))
        self.home.symlink_to(real)
        with self.assertRaises(SystemExit):
            run('ls', '--json')

    def test_4_put_refuses_a_symlinked_entry(self):
        run('put', 'job', 'seed')
        outside = Path(self.dir.name) / 'outside.json'
        outside.write_text(json.dumps({'title': 'secret'}))
        link = self.home / 'job' / 'x.json'
        link.symlink_to(outside)
        with self.assertRaises(SystemExit):
            run('put', 'job', 'x', '--now', 'updated')
        self.assertTrue(link.is_symlink())

    def test_5_put_keeps_a_corrupt_entry(self):
        path = self.write_raw('job', 'x', '{"title":"preserve",')
        with self.assertRaises(SystemExit):
            run('put', 'job', 'x', '--now', 'new')
        self.assertEqual(path.read_text(), '{"title":"preserve",')

    def test_6_bad_field_types_are_skipped_with_a_warning(self):
        run('put', 'job', 'good')
        self.write_raw('job', 'bad', json.dumps({'source': 'job', 'id': 'bad', 'updated': 'yesterday'}))
        code, out = run('ls', '--json')
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIn('good', [r['id'] for r in data['items']])
        self.assertNotIn('bad', [r['id'] for r in data['items']])
        self.assertTrue(any('bad' in w for w in data['warnings']))

    def test_7_non_finite_seconds_are_refused(self):
        run('put', 'job', 'x')
        for argv in (('done', 'job', 'x', '--keep', 'nan'), ('done', 'job', 'x', '--keep', '-1'),
                     ('put', 'job', 'x', '--interval', 'nan'), ('put', 'job', 'x', '--interval', 'inf')):
            with self.assertRaises(SystemExit):
                run(*argv)
        code, out = run('ls', '--json')
        json.loads(out, parse_constant=lambda c: self.fail(f'non-standard JSON {c}'))

    def test_8_ls_source_follows_the_name_rule(self):
        with self.assertRaises(SystemExit):
            run('ls', '--source', '../escape')


class Status(Base):
    def status(self):
        code, out = run('status', '--json')
        return code, json.loads(out)

    def test_empty_bus_is_not_an_error(self):
        code, data = self.status()
        self.assertEqual((code, data['problem'], data['sources'], data['readers']), (0, None, [], []))
        code, out = run('status')
        self.assertIn('not created yet', out)

    def test_writers_and_readers_are_listed(self):
        run('put', 'mac', 'slow-log', '--interval', '300', '--state', 'warn')
        run('put', 'xverify', 'r1')
        run('done', 'xverify', 'r1', '--exit-code', '0')
        run('ls', '--json', '--reader', 'aitop')
        code, data = self.status()
        self.assertEqual(code, 0)
        self.assertEqual([(s['source'], s['entries'], s['status']) for s in data['sources']],
                         [('mac', 1, {'warn': 1}), ('xverify', 1, {'finished': 1})])
        self.assertEqual([r['reader'] for r in data['readers']], ['aitop'])
        # The reader record is not mistaken for a source.
        self.assertNotIn('.readers', [s['source'] for s in data['sources']])
        code, out = run('status')
        self.assertIn('aitop', out)
        self.assertIn('last read', out)

    def test_reader_record_is_throttled_and_name_checked(self):
        run('ls', '--reader', 'aitop')
        path = self.home / '.readers' / 'aitop.json'
        first = path.read_text()
        run('ls', '--reader', 'aitop')
        self.assertEqual(path.read_text(), first)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaises(SystemExit):
            run('ls', '--reader', '../x')

    def test_refused_bus_is_reported_with_exit_1(self):
        real = Path(self.dir.name) / 'real'
        real.mkdir()
        self.home.symlink_to(real)
        code, data = self.status()
        self.assertEqual(code, 1)
        self.assertIn('symlink', data['problem'])


class RecheckFindings(Base):
    """Cases from the re-verification: entries someone else damaged by hand."""

    def raw(self, text, item='x'):
        run('put', 'job', 'good')
        path = self.home / 'job' / f'{item}.json'
        path.write_text(text)
        return path

    def listed(self):
        code, out = run('ls', '--json')
        self.assertEqual(code, 0)
        data = json.loads(out, parse_constant=lambda c: self.fail(f'non-standard JSON {c}'))
        return [r['id'] for r in data['items']], data['warnings']

    @unittest.skipIf(os.getuid() == 0, 'root reads a 000 folder anyway')
    def test_n1_unreadable_folder_is_a_warning(self):
        run('put', 'other', 'a')
        os.chmod(self.home / 'other', 0)
        self.addCleanup(os.chmod, self.home / 'other', 0o700)
        ids, warnings = self.listed()
        self.assertNotIn('a', ids)
        self.assertTrue(any('other' in w for w in warnings), warnings)

    def test_n2_wrong_container_types_are_malformed(self):
        path = self.raw(json.dumps({'source': 'job', 'id': 'x', 'links': None}))
        ids, warnings = self.listed()
        self.assertNotIn('x', ids)
        with self.assertRaises(SystemExit):
            run('put', 'job', 'x', '--link', 'report=/tmp/r')
        self.assertEqual(json.loads(path.read_text())['links'], None)

    def test_n3_out_of_range_values_are_malformed(self):
        for bad in ({'progress': [9, 0]}, {'progress': [-1, 2]}, {'ai': '../bad'}, {'interval': -1},
                    {'pid': 0}, {'state': 'exploded'}):
            with self.subTest(bad=bad):
                self.raw(json.dumps(dict({'source': 'job', 'id': 'x'}, **bad)))
                ids, warnings = self.listed()
                self.assertNotIn('x', ids)
                self.assertTrue(warnings)

    def test_n4_nan_anywhere_is_unreadable(self):
        self.raw('{"source": "job", "id": "x", "schema": NaN}')
        ids, warnings = self.listed()
        self.assertEqual(ids, ['good'])

    def test_n5_huge_numbers_are_malformed(self):
        self.raw(json.dumps({'source': 'job', 'id': 'x', 'updated': 10 ** 400}))
        ids, warnings = self.listed()
        self.assertEqual(ids, ['good'])

    def test_n6_empty_object_is_not_overwritten(self):
        path = self.raw('{}')
        with self.assertRaises(SystemExit):
            run('put', 'job', 'x', '--now', 'new')
        self.assertEqual(path.read_text(), '{}')

    def test_n7_gc_skips_an_entry_swapped_for_a_symlink(self):
        run('put', 'job', 'x')
        path = self.home / 'job' / 'x.json'
        outside = Path(self.dir.name) / 'old.json'
        outside.write_text(json.dumps({'source': 'job', 'id': 'x', 'expires': 1.0, 'updated': 1.0}))
        real_locked = aibus.locked

        @contextlib.contextmanager
        def swapped_while_waiting(folder):
            path.unlink()
            path.symlink_to(outside)
            with real_locked(folder):
                yield
        aibus.locked = swapped_while_waiting
        try:
            code, out = run('gc', '--days', '1')
        finally:
            aibus.locked = real_locked
        self.assertEqual(out.strip(), 'removed 0')
        self.assertTrue(path.is_symlink())


if __name__ == '__main__':
    unittest.main()
