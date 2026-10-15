"""Parse reviewed Android sources and compile/run the exact reader contract.

This is a focused JVM check, not an Android assemble or device test.
"""
import json,subprocess,tempfile,urllib.request,zipfile
from pathlib import Path
from validate_betting_favourite_policy import DEPENDENCIES,SYNTAX
ROOT=Path(__file__).resolve().parents[1]

def main():
    contract=json.loads((ROOT/'reports/betting_v2/reviewed_exact_uat_contract.json').read_text())
    with tempfile.TemporaryDirectory(prefix='exact-uat-') as directory:
        work=Path(directory);deps=work/'deps';deps.mkdir()
        for artifact in DEPENDENCIES:
            target=deps/Path(artifact).name
            with urllib.request.urlopen('https://repo.maven.apache.org/maven2/'+artifact,timeout=30) as response:
                target.write_bytes(response.read())
            if not zipfile.is_zipfile(target):raise ValueError('Invalid dependency')
        paths=[]
        for relative,source in contract['sources'].items():
            target=work/relative;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(source);paths.append(str(target))
        helper=work/'KotlinSyntax.java';helper.write_text(SYNTAX)
        subprocess.run(['java','--class-path',str(deps/'*'),str(helper),*paths],check=True)
        tests=work/'ExactReaderContract.kt';tests.write_text(contract['tests'])
        cp=str(deps/'kotlin-stdlib-1.9.24.jar')+':'+str(deps/'annotations-13.0.jar')
        model=work/'app/src/main/java/com/statmaker/app/DirectForecastContract.kt'
        classes=work/'classes'
        subprocess.run(['java','-cp',str(deps/'*'),'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
            '-no-stdlib','-no-reflect','-classpath',cp,'-d',str(classes),str(model),str(tests)],check=True)
        subprocess.run(['java','-cp',str(classes)+':'+cp,'com.statmaker.app.ExactReaderContractKt'],check=True)

if __name__=='__main__':main()
