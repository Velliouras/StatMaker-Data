import contextlib,io,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2 import research_checkpoint as checkpoint

class CheckpointIntegrityTest(unittest.TestCase):
    def fixture(self,root):
        (root/'scripts/betting_v2').mkdir(parents=True);(root/'tests').mkdir()
        (root/'scripts/betting_v2/research_example.py').write_text('value=1\n')
        out=root/'results';out.mkdir();(out/'report.json').write_text('{"complete":true}')
        target=root/'archive.zip'
        with zipfile.ZipFile(target,'w') as z:z.writestr('previous.txt','previous evidence')
        os.setxattr(target,'user.library-file-version',b'14')
        return out,target

    def test_atomic_replacement_is_valid_and_preserves_confirmed_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);out,target=self.fixture(root)
            with patch.multiple(checkpoint,ROOT=root,TARGET=target,LIBRARY_ID=None,HELPER=None),contextlib.redirect_stdout(io.StringIO()):
                checkpoint.persist(out,'complete')
            with zipfile.ZipFile(target) as z:
                self.assertIsNone(z.testzip());self.assertIn('reports/report.json',z.namelist())
            self.assertEqual(os.getxattr(target,'user.library-file-version'),b'14')

    def test_failed_build_keeps_previous_evidence_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);out,target=self.fixture(root);before=target.read_bytes()
            with patch.multiple(checkpoint,ROOT=root,TARGET=target,LIBRARY_ID=None,HELPER=None),patch.object(zipfile.ZipFile,'write',side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):checkpoint.persist(out,'incomplete')
            self.assertEqual(target.read_bytes(),before);self.assertFalse(target.with_name('archive.zip.tmp').exists())

    def test_success_response_cannot_hide_local_byte_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);out,target=self.fixture(root)
            def upload(*args,**kwargs):
                request=json.loads(kwargs['input']);self.assertEqual(request['uploads'][0]['expected_current_version'],14)
                target.write_bytes(b'truncated')
                return SimpleNamespace(stdout=json.dumps({'results':[{'status':'succeeded','current_version_number':15}]}))
            with patch.multiple(checkpoint,ROOT=root,TARGET=target,LIBRARY_ID='test-id',HELPER='test-helper'),patch.object(checkpoint.subprocess,'run',side_effect=upload):
                with self.assertRaisesRegex(RuntimeError,'changed during save'):checkpoint.persist(out,'complete')

if __name__=='__main__':unittest.main()
