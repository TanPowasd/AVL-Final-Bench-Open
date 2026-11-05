import json
import shutil
import tempfile
import unittest
from pathlib import Path

from organizer.rift_era.engine.catalog import Catalog, DEFAULT_DATA, RULE_FILES
from organizer.rift_era.engine.errors import InfrastructureFault


class RuleVersionTests(unittest.TestCase):
    def test_shipped_tables_share_one_rule_version(self):
        catalog = Catalog()
        for name in ('scoring', 'coupling', 'military'):
            self.assertEqual(catalog.tables[name]['version'], catalog.version)

    def test_any_stale_table_version_is_rejected(self):
        for name in ('scoring', 'coupling', 'military'):
            with self.subTest(table=name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                for rule_file in RULE_FILES:
                    shutil.copy(DEFAULT_DATA / f'{rule_file}.json', directory)
                path = directory / f'{name}.json'
                table = json.loads(path.read_text(encoding='utf-8'))
                table['version'] = 'rift_era_rules_stale'
                path.write_text(json.dumps(table, ensure_ascii=False), encoding='utf-8')
                with self.assertRaisesRegex(InfrastructureFault, f'{name} version mismatch'):
                    Catalog(directory)


if __name__ == '__main__':
    unittest.main()
