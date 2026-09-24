"""Capture coherent copies from a connected debuggable Android installation. No root."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

DATABASES = ('statmaker_model_performance.db', 'statmaker_prepared_betting.db')

def collect(adb, serial, package, output, stop_app):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]+', package):
        raise ValueError('Invalid package name')
    if not stop_app:
        raise ValueError('Use --stop-app: the app must close before capturing both databases coherently')
    output = Path(output)
    if output.exists():
        raise ValueError('Choose a new output directory')
    prefix = [adb] + (['-s', serial] if serial else [])
    def run(*args):
        return subprocess.run([*prefix,*args],capture_output=True,check=True,timeout=60).stdout
    run('get-state')
    run('shell','run-as',package,'id') # Check permission before closing the application.
    version = run('shell','dumpsys','package',package).decode(errors='replace')
    run('shell','am','force-stop',package)
    with tempfile.TemporaryDirectory(prefix='statmaker-device-') as temporary:
        temporary = Path(temporary)
        for name in DATABASES:
            for suffix in ('','-wal'):
                remote='databases/'+name+suffix
                exists=subprocess.run([*prefix,'shell','run-as',package,'test','-f',remote],capture_output=True,timeout=30)
                if exists.returncode:
                    if suffix: continue
                    raise ValueError('Missing Android database: '+name)
                (temporary/(name+suffix)).write_bytes(run('exec-out','run-as',package,'cat',remote))
        # Merge WAL only in local temporary copies; write nothing into the Android app.
        output.mkdir(parents=True)
        for name in DATABASES:
            source=sqlite3.connect(temporary/name)
            destination=sqlite3.connect(output/name)
            try:
                source.backup(destination)
                if destination.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    raise ValueError('Captured SQLite database failed integrity validation')
            finally:
                source.close(); destination.close()
        info={'capturedAt':datetime.now(timezone.utc).isoformat(),'package':package,
              'versionLines':[line.strip() for line in version.splitlines() if 'versionCode=' in line or 'versionName=' in line],
              'files':{name:hashlib.sha256((output/name).read_bytes()).hexdigest() for name in DATABASES}}
        (output/'capture.json').write_text(json.dumps(info,indent=2)+'\n')
    print('Captured databases. Reopen StatMaker normally. No app data was cleared or modified.')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--adb',default='adb');p.add_argument('--serial')
    p.add_argument('--package',default='com.statmaker.app');p.add_argument('--output',type=Path,required=True)
    p.add_argument('--stop-app',action='store_true')
    a=p.parse_args();collect(a.adb,a.serial,a.package,a.output,a.stop_app)
