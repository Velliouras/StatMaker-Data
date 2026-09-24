"""Compile the pinned Android classifier unchanged, without Android SDK or Gradle."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parent
LOCK = json.loads((ROOT / 'source-lock.json').read_text())

def digest(data):
    return hashlib.sha256(data).hexdigest()

def source(root, name):
    path = root / name
    if not path.is_file():
        path = root / 'app/src' / ('test' if name.endswith('Test.kt') else 'main') / 'java/com/statmaker/app' / name
    data = (path.read_text(encoding="utf-8").rstrip() + "\n").encode("utf-8")
    if digest(data) != LOCK['sources'][name]:
        raise ValueError(f'Android source mismatch: {name}; review and repin before export')
    return data.decode()

def block(text, marker):
    start = text.index(marker)
    opened = text.index('{', start)
    depth = 1
    index = opened + 1
    while depth:
        depth += (text[index] == '{') - (text[index] == '}')
        index += 1
    return text[start:index]

def build(android_source, output, deps):
    android_source, output, deps = map(Path, (android_source, output, deps))
    output.mkdir(parents=True, exist_ok=True)
    deps.mkdir(parents=True, exist_ok=True)
    sources = {name: source(android_source, name) for name in LOCK['sources']}
    jars = []
    for path, expected in LOCK['dependencies'].items():
        target = deps / path.rsplit('/', 1)[-1]
        if not target.exists():
            with urllib.request.urlopen('https://repo.maven.apache.org/maven2/' + path, timeout=90) as response:
                data = response.read()
            if digest(data) != expected:
                raise ValueError(f'Dependency checksum mismatch: {target.name}')
            target.write_bytes(data)
        if digest(target.read_bytes()) != expected:
            raise ValueError(f'Dependency checksum mismatch: {target.name}')
        jars.append(str(target.resolve()))
    confidence = sources['ModelPerformanceConfidence.kt'].split('internal class ModelPerformanceConfidenceRepository')[0]
    confidence = confidence.replace('import android.content.Context\n', '')
    (output / 'Confidence.kt').write_text(confidence)
    model = 'package com.statmaker.app\n' + block(sources['ModelPerformanceFeature.kt'], 'internal data class ModelPerformanceEntry(')
    model += '\ninternal enum class MyBetLegOutcome { WON, HALF_WON, HALF_LOST, LOST, VOID, PENDING }\n'
    model += block(sources['StrictBetRules.kt'], 'object StrictBetRules {')
    model = model.replace('package com.statmaker.app\n', 'package com.statmaker.app\nimport kotlin.math.ceil\n')
    (output / 'Model.kt').write_text(model)
    market = sources['MarketIdentity.kt'].split('fun normalizedMarketIdentity(selection:')[0]
    market += block(sources['MarketIdentity.kt'], 'private fun canonicalMarketKey(') + '\n'
    market += block(sources['MarketIdentity.kt'], 'fun canonicalMarketFamilyLabel(') + '\n'
    (output / 'Market.kt').write_text(market)
    predicate = 'package com.statmaker.app\n' + block(sources['ModelPerformanceAnalytics.kt'], 'internal fun isDefaultModelPerformanceEntry(')
    (output / 'Predicate.kt').write_text(predicate)
    (output / 'AndroidConfidenceTest.kt').write_text(sources['ModelPerformanceConfidenceTest.kt'])
    # Context assembly is the production function verbatim; only I/O boundary types are adapters.
    context = 'package com.statmaker.app\n' + block(sources['ModelPerformanceConfidence.kt'], 'internal fun modelPerformanceConfidenceContext(')
    (output / 'Context.kt').write_text(context)
    classpath = os.pathsep.join(jars)
    files = [str(p) for p in output.glob('*.kt')] + [str(ROOT / 'Runner.kt')]
    jar = output / 'classifier.jar'
    subprocess.run(['java', '-Xmx768m', '-cp', classpath, 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
                    '-no-stdlib', '-no-reflect', '-jvm-target', '17', '-classpath', classpath,
                    '-d', str(jar), *files], check=True, timeout=120)
    runtime = os.pathsep.join([str(jar.resolve()), *jars])
    subprocess.run(['java', '-cp', runtime, 'org.junit.runner.JUnitCore',
                    'com.statmaker.app.ModelPerformanceConfidenceTest'], check=True, timeout=60)
    result = {'classpath': runtime, 'androidCommit': LOCK['androidCommit'],
              'sourceLockSha256': digest((ROOT / 'source-lock.json').read_bytes()),
              'jarSha256': digest(jar.read_bytes())}
    (output / 'runtime.json').write_text(json.dumps(result, indent=2) + '\n')
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--android-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dependencies', type=Path, required=True)
    args = parser.parse_args()
    build(args.android_source, args.output, args.dependencies)
