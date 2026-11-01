import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('health', Path(__file__).resolve().parents[1]/'health.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def meal(kcal=100):
    return {'metrics':{'kcal':{'low':kcal,'high':kcal+20}},'provenance':{'method':'user_report'}}


class DiaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'diary.sqlite3'
        self.db = h.connect(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add(self, source='test:1', payload=None, kind='meal', day='2026-01-01'):
        return h.add(self.db, kind, day, payload or meal(), source)['record']

    def test_dedup_and_conflict(self):
        a = self.add()
        self.assertEqual(h.add(self.db,'meal','2026-01-01',meal(),'test:1')['record'], a)
        with self.assertRaises(ValueError): self.add(payload=meal(200))
        self.assertEqual(len(h.records(self.db)),1)

    def test_edit_delete_audit_and_stale(self):
        a = self.add()
        b = h.edit(self.db,a['id'],meal(200),a['updated_at'])
        with self.assertRaises(ValueError): h.edit(self.db,a['id'],meal(300),a['updated_at'])
        h.delete(self.db,b['id'],b['updated_at'])
        self.assertEqual(h.records(self.db),[])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],3)
        with self.assertRaises(ValueError): self.add()

    def test_ranges_missing_and_exercise_not_double_counted(self):
        self.assertIsNone(h.daily_summary(self.db,'2026-01-01')['intake']['kcal']['range'])
        self.assertIsNone(h.daily_summary(self.db,'2026-01-01')['calorie_deficit_kcal'])
        self.add()
        self.add('test:work',{'metrics':{'exercise_kcal':{'low':200,'high':300}},'provenance':{'method':'device'}},'workout')
        self.add('test:daily',{'metrics':{'expenditure_kcal':{'low':2000,'high':2200}},'intake_complete':True,'expenditure_basis':'total_including_activity_and_exercise','provenance':{'method':'device'}},'daily')
        day=h.daily_summary(self.db,'2026-01-01')
        self.assertEqual(day['energy_balance_kcal'],{'low':-2100,'high':-1880})
        self.assertEqual(day['calorie_deficit_kcal'],{'low':1880,'high':2100})
        self.assertFalse(day['intake']['protein_g']['complete'])
        self.assertFalse(day['exercise_added_to_expenditure'])
        with self.assertRaises(ValueError): self.add('duplicate-day',{'provenance':{'method':'user_report'}},'daily')

    def test_explicit_zero(self):
        self.add('day-zero',{'intake_complete':True,'provenance':{'method':'user_report'}},'daily')
        s=h.daily_summary(self.db,'2026-01-01')
        self.assertEqual(s['intake']['kcal']['range'],{'low':0,'high':0})
        self.assertTrue(s['intake']['kcal']['complete'])

    def test_validation(self):
        for value in (-1,float('nan'),float('inf'),True):
            with self.assertRaises(ValueError): self.add(payload=meal(value))
        with self.assertRaises(ValueError): self.add(day='2026-1-1')
        with self.assertRaises(ValueError): self.add(payload={'metrics':{},'provenance':{'method':'agent_estimate'}})
        with self.assertRaises(ValueError): self.add(payload={'metrics':{'expenditure_kcal':{'low':1,'high':2}},'provenance':{'method':'device'}},kind='daily')
        with self.assertRaises(ValueError): h.database_path(Path(h.__file__).parent/'private.sqlite3')
        with self.assertRaises(ValueError): h.add(self.db,'meal','2026-01-01',meal(),'')

    def test_trends_exclude_missing(self):
        self.add()
        self.add('complete',{'intake_complete':True,'provenance':{'method':'user_report'}},'daily')
        result=h.trends(self.db,'2026-01-01','2026-01-03')
        self.assertEqual(result['complete_intake_days'],1)
        self.assertEqual(result['mean_kcal_complete_days'],{'low':100,'high':120})
        self.assertEqual(len(result['days']),3)

    def test_backup_restore_and_conflicts(self):
        a=self.add()
        archive=self.root/'backup.zip'
        h.create_backup(self.path,archive)
        self.assertTrue(h.inspect_backup(archive)['valid'])
        target=self.root/'restored.sqlite3'
        result=h.restore_backup(archive,target,'replace')
        self.assertEqual(result['imported'],1)
        with h.connect(target) as conn:
            self.assertEqual(h.get(conn,a['id']),a)
            h.edit(conn,a['id'],meal(300),a['updated_at'])
        result=h.restore_backup(archive,target,'merge')
        self.assertEqual(len(result['conflicts']),1)
        self.assertTrue(Path(result['prior_backup']).is_file())
        with h.connect(target) as conn:
            self.assertEqual(h.get(conn,a['id'])['payload'],meal(300))
        h.restore_backup(archive,target,'replace')
        with h.connect(target) as conn:
            self.assertEqual(h.get(conn,a['id']),a)
        with self.assertRaises(ValueError): h.restore_backup(archive,target,None)
        with self.assertRaises(ValueError): h.create_backup(self.path,archive)

    def test_merge_skips_identical_and_imports_new(self):
        self.add()
        archive=self.root/'first.zip'; h.create_backup(self.path,archive)
        target=self.root/'target.sqlite3'; h.restore_backup(archive,target,'merge')
        self.add('test:2')
        archive2=self.root/'second.zip'; h.create_backup(self.path,archive2)
        result=h.restore_backup(archive2,target,'merge')
        self.assertEqual((result['imported'],result['skipped_identical']),(1,1))

    def test_archive_tamper_and_unsafe_names(self):
        self.add(); archive=self.root/'valid.zip'; h.create_backup(self.path,archive)
        with zipfile.ZipFile(archive) as z: contents={n:z.read(n) for n in z.namelist()}
        for name,data in [('data.json',b'{}'),('../escape',b'bad')]:
            altered=dict(contents); altered[name]=data
            bad=self.root/('bad'+str(len(name))+'.zip')
            with zipfile.ZipFile(bad,'w') as z:
                for n,b in altered.items(): z.writestr(n,b)
            with self.assertRaises(ValueError): h.inspect_backup(bad)
        self.assertFalse((self.root.parent/'escape').exists())

    def test_modified_schema_rejected(self):
        self.db.execute('DROP TABLE audit')
        self.db.execute('CREATE TABLE audit(id TEXT PRIMARY KEY NOT NULL,record_id TEXT,action TEXT,before_json TEXT,after_json TEXT,at TEXT)')
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'schema'):
            h.create_backup(self.path,self.root/'bad-schema.zip')

    def test_manifest_shape_json_error(self):
        archive=self.root/'valid.zip'; h.create_backup(self.path,archive)
        with zipfile.ZipFile(archive) as z: contents={n:z.read(n) for n in z.namelist()}
        for bad_manifest in ([], {'files':[]}, {'format':'health-agent-backup','format_version':1,'schema_version':'1','files':{name:[] for name in h.FILES}}):
            contents['manifest.json']=json.dumps(bad_manifest).encode()
            bad=self.root/'bad-manifest.zip'
            with zipfile.ZipFile(bad,'w') as z:
                for n,b in contents.items(): z.writestr(n,b)
            result=subprocess.run([sys.executable,h.__file__,'--db',str(self.path)],input=json.dumps({'action':'inspect','zip_path':str(bad)}),text=True,capture_output=True)
            self.assertEqual(result.returncode,1)
            self.assertFalse(json.loads(result.stdout)['ok'])
            self.assertEqual(result.stderr,'')

    def test_numeric_overflow_rejected(self):
        for number in (1e308, 10**400):
            with self.assertRaises(ValueError): self.add(payload=meal(number))
            request={'action':'add','kind':'meal','day':'2026-01-01','payload':meal(number),'source_id':'huge'}
            result=subprocess.run([sys.executable,h.__file__,'--db',str(self.path)],input=json.dumps(request),text=True,capture_output=True)
            self.assertEqual(result.returncode,1)
            self.assertFalse(json.loads(result.stdout)['ok'])
            self.assertEqual(result.stderr,'')

    def test_cli(self):
        cmd=[sys.executable,h.__file__,'--db',str(self.path)]
        result=subprocess.run(cmd,input='{"action":"daily","day":"2026-01-01"}',text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        result=subprocess.run(cmd,input='{"action":"nope"}',text=True,capture_output=True)
        self.assertEqual(result.returncode,1)
        self.assertFalse(json.loads(result.stdout)['ok'])


if __name__ == '__main__': unittest.main()
