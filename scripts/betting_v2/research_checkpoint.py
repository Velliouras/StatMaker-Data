"""Persist expensive offline experiment checkpoints after each completed fold."""
import hashlib,json,os,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
TARGET=Path(os.environ.get('STATMAKER_RESEARCH_ARCHIVE',str(ROOT.parent/'StatMaker_Chronological_Research_20261016.zip')))
LIBRARY_ID=os.environ.get('STATMAKER_RESEARCH_LIBRARY_ID')
HELPER=os.environ.get('STATMAKER_LIBRARY_UPLOAD_HELPER')

def persist(out,status):
    # Validate a separate completed file before replacing the durable identity.
    # A failed or interrupted build must leave the previous archive intact.
    temporary=TARGET.with_name(TARGET.name+'.tmp')
    try:
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for pattern in ('chronological_stack.py','research_*.py','expanded_quote_replay.py','summarize_oof_research.py','freeze_forward_research.py','score_forward_research.py'):
                for p in (ROOT/'scripts/betting_v2').glob(pattern):z.write(p,p.relative_to(ROOT))
            for p in (ROOT/'tests').glob('test_*research*.py'):z.write(p,p.relative_to(ROOT))
            for p in sorted(out.iterdir()):
                if p.is_file() and p.name!='running.lock' and p.suffix!='.tmp':z.write(p,'reports/'+p.name)
            z.writestr('STATUS.txt',status+'\nNo live UAT or PROD change. Baseline evidence is in StatMaker_Corrected_Training_20261015.zip.\n')
        with zipfile.ZipFile(temporary) as z:
            bad=z.testzip()
            if bad is not None:raise ValueError('Invalid research archive entry: '+bad)
        expected_hash=hashlib.sha256(temporary.read_bytes()).hexdigest()
        if TARGET.exists():
            for name in os.listxattr(TARGET):os.setxattr(temporary,name,os.getxattr(TARGET,name))
        os.replace(temporary,TARGET)
    finally:temporary.unlink(missing_ok=True)
    if not LIBRARY_ID or not HELPER:
        print('LOCAL_CHECKPOINT',str(TARGET),status,flush=True)
        return
    version=int(os.getxattr(TARGET,'user.library-file-version').decode())
    request=dict(uploads=[dict(local_path=str(TARGET),purpose='replace_library_file',library_file_id=LIBRARY_ID,
        expected_current_version=version,version_reason=status)])
    result=subprocess.run([sys.executable,HELPER],input=json.dumps(request),text=True,capture_output=True,check=True)
    lines=result.stdout.splitlines();payload=json.loads(lines[-1]);saved=payload['results'][0]
    if saved['status']!='succeeded':raise RuntimeError('Research checkpoint not saved')
    if hashlib.sha256(TARGET.read_bytes()).hexdigest()!=expected_hash:
        raise RuntimeError('Research archive changed during save; do not treat the local file as verified')
    print('DURABLE_CHECKPOINT',saved.get('current_version_number'),status,flush=True)
