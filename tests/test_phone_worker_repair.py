import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "phone_worker_repair", Path(__file__).resolve().parents[1] / "ocr" / "repair_phone_worker.py"
)
REPAIR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPAIR)


class PhoneWorkerRepairTests(unittest.TestCase):
    def supported_source(self):
        return '\n'.join((
            f'WORKER_VERSION = "{REPAIR.OLD_VERSION}"',
            'def recognize(frames, data, MAX_BYTES):',
            '    if frames > 200 or len(data) > MAX_BYTES:',
            '        return "per_frame"',
            '    return "whole_animation"',
            'def budget(depth, height):',
            '    return 512 if depth > 0 and height <= 1024 else None',
            'def ordering(only_failed):',
            '    order = "priority,attempts,rowid" if only_failed else "priority,rowid"',
            '    return order',
            '',
        ))

    def test_100_frame_animation_uses_individual_requests(self):
        before = {}
        after = {}
        exec(self.supported_source(), before)
        exec(REPAIR.repaired_source(self.supported_source()), after)
        self.assertEqual(before['recognize'](100, b'webp', 100000), 'whole_animation')
        self.assertEqual(after['recognize'](100, b'webp', 100000), 'per_frame')
        self.assertEqual(after['recognize'](1, b'png', 100000), 'whole_animation')
        self.assertIsNotNone(after['budget'](0, 512))
        self.assertIsNone(after['budget'](0, 1800))

    def test_transiently_failing_file_cannot_block_unattempted_files(self):
        namespace = {}
        exec(REPAIR.repaired_source(self.supported_source()), namespace)
        with sqlite3.connect(':memory:') as db:
            db.execute('create table jobs(name text, priority integer, attempts integer)')
            db.executemany('insert into jobs values(?,?,?)', (
                ('stalled_animation', 10, 241), ('photo', 10, 0), ('document', 0, 0),
            ))
            query = 'select name from jobs order by ' + namespace['ordering'](False)
            self.assertEqual([row[0] for row in db.execute(query)],
                             ['document', 'photo', 'stalled_animation'])

    def test_rerunning_deployment_preserves_repaired_source(self):
        repaired = REPAIR.repaired_source(self.supported_source())
        self.assertEqual(REPAIR.repaired_source(repaired), repaired)

    def test_rollback_restores_the_original_worker(self):
        original = self.supported_source()
        repaired = REPAIR.repaired_source(original)
        self.assertEqual(REPAIR.repaired_source(repaired, rollback=True), original)
        self.assertEqual(REPAIR.repaired_source(original, rollback=True), original)

    def test_unknown_or_partial_revision_does_not_write(self):
        for source in (self.supported_source().replace('frames > 200', 'frames > 150'),
                       self.supported_source().replace(REPAIR.OLD_VERSION, REPAIR.NEW_VERSION),
                       self.supported_source() + '\ndef invalid(:\n'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'worker.py'
                path.write_text(source, encoding='utf-8')
                with self.assertRaises((ValueError, SyntaxError)):
                    REPAIR.repair(path)
                self.assertEqual(path.read_text(encoding='utf-8'), source)
                self.assertFalse(path.with_name('worker.py.frame-repair.tmp').exists())


if __name__ == '__main__':
    unittest.main()
