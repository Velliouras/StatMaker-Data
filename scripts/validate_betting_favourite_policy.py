"""Validate reviewed UAT Kotlin without Android, emulators or betting-provider calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / 'reports/betting_v2/reviewed_favourite_policy.json'
DEPENDENCIES = [
    'org/jetbrains/kotlin/kotlin-compiler-embeddable/1.9.24/kotlin-compiler-embeddable-1.9.24.jar',
    'org/jetbrains/kotlin/kotlin-stdlib/1.9.24/kotlin-stdlib-1.9.24.jar',
    'org/jetbrains/kotlin/kotlin-reflect/1.6.10/kotlin-reflect-1.6.10.jar',
    'org/jetbrains/kotlin/kotlin-script-runtime/1.9.24/kotlin-script-runtime-1.9.24.jar',
    'org/jetbrains/kotlin/kotlin-daemon-embeddable/1.9.24/kotlin-daemon-embeddable-1.9.24.jar',
    'org/jetbrains/intellij/deps/trove4j/1.0.20200330/trove4j-1.0.20200330.jar',
    'org/jetbrains/annotations/13.0/annotations-13.0.jar',
]
SYNTAX = '''
import java.nio.file.*;
import org.jetbrains.kotlin.com.intellij.openapi.util.Disposer;
import org.jetbrains.kotlin.com.intellij.psi.PsiErrorElement;
import org.jetbrains.kotlin.com.intellij.psi.util.PsiTreeUtil;
import org.jetbrains.kotlin.cli.jvm.compiler.KotlinCoreEnvironment;
import org.jetbrains.kotlin.cli.jvm.compiler.EnvironmentConfigFiles;
import org.jetbrains.kotlin.config.CompilerConfiguration;
import org.jetbrains.kotlin.psi.KtPsiFactory;
public class KotlinSyntax {
  public static void main(String[] args) throws Exception {
    var disposable = Disposer.newDisposable();
    try {
      var env = KotlinCoreEnvironment.createForProduction(disposable, new CompilerConfiguration(), EnvironmentConfigFiles.JVM_CONFIG_FILES);
      var factory = new KtPsiFactory(env.getProject(), false);
      for (String p : args) {
        var path = Path.of(p);
        var file = factory.createFile(path.getFileName().toString(), Files.readString(path));
        var errors = PsiTreeUtil.collectElementsOfType(file, PsiErrorElement.class);
        if (!errors.isEmpty()) throw new IllegalStateException(p + ": " + errors);
        System.out.println("PASS Kotlin syntax: " + p);
      }
    } finally { Disposer.dispose(disposable); }
  }
}
'''

def main():
    contract = json.loads(CONTRACT.read_text())
    with tempfile.TemporaryDirectory(prefix='favourite-policy-') as directory:
        work = Path(directory)
        deps = work / 'dependencies'
        deps.mkdir()
        for artifact in DEPENDENCIES:
            target = deps / Path(artifact).name
            with urllib.request.urlopen('https://repo.maven.apache.org/maven2/' + artifact, timeout=30) as source:
                target.write_bytes(source.read())
            if not zipfile.is_zipfile(target):
                raise ValueError('Invalid Kotlin dependency: ' + target.name)
        paths = []
        for relative, source in contract['sources'].items():
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
            paths.append(str(target))
            print('Reviewed source SHA256:', relative, hashlib.sha256(source.encode()).hexdigest(), flush=True)
        helper = work / 'KotlinSyntax.java'
        helper.write_text(SYNTAX)
        subprocess.run(['java', '--class-path', str(deps / '*'), str(helper), *paths], check=True)
        stubs = work / 'Stubs.kt'
        stubs.write_text(contract['stubs'])
        tests = work / 'FavouritePolicyContract.kt'
        tests.write_text(contract['tests'])
        policy = work / 'app/src/main/java/com/statmaker/app/BettingFavouritePolicy.kt'
        classes = work / 'classes'
        classpath = str(deps / 'kotlin-stdlib-1.9.24.jar') + ':' + str(deps / 'annotations-13.0.jar')
        subprocess.run(['java', '-cp', str(deps / '*'), 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
            '-no-stdlib', '-no-reflect', '-classpath', classpath, '-d', str(classes),
            str(policy), str(stubs), str(tests)], check=True)
        subprocess.run(['java', '-cp', str(classes) + ':' + classpath,
            'com.statmaker.app.FavouritePolicyContractKt'], check=True)

if __name__ == '__main__':
    main()
