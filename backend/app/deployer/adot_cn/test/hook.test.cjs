// N17-obs465 regression matrix: launchpad-adot-cn.cjs against every supported ADOT Node version
// (package.json aliases, lockfile-pinned). It FAILS LOUDLY when a release moves an internal the
// hook depends on, and it pins the auto-disable path (ADOT accepting .cn natively).
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const HERE = path.resolve(__dirname, '..');
const HOOK = path.join(HERE, 'launchpad-adot-cn.cjs');
const VERSIONS = Object.keys(require(path.join(HERE, 'package.json')).devDependencies)
  .filter((name) => name.startsWith('adot-node-'));
const ADOT = '@aws/aws-distro-opentelemetry-node-autoinstrumentation';

// an "app" whose node_modules holds one ADOT version under its real name
function appWith(alias) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `adot-cn-${alias}-`));
  fs.mkdirSync(path.join(dir, 'node_modules', '@aws'), { recursive: true });
  fs.symlinkSync(path.join(HERE, 'node_modules', alias), path.join(dir, 'node_modules', ADOT), 'dir');
  return dir;
}

// what the platform's layer runs: NODE_OPTIONS preload, then the app's own `--require register`
const PROBE = `
setTimeout(() => {
  const api = require(require.resolve('@opentelemetry/api', { paths: [require('node:path').dirname(require.resolve('${ADOT}/register'))] }));
  const d = api.trace.getTracerProvider().getDelegate();
  const ps = (d._activeSpanProcessor && d._activeSpanProcessor._spanProcessors) || [];
  console.log('PROBE ' + JSON.stringify(ps.filter((p) => p._exporter).map((p) => ({
    cls: p._exporter.constructor.name, url: p._exporter.endpoint || null,
    region: (p._exporter.authenticator && p._exporter.authenticator.region) || null }))));
  console.log('ENV ' + JSON.stringify({ traces: process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, logs: process.env.OTEL_LOGS_EXPORTER }));
}, 300);`;

function run(app, env, preloads = [HOOK]) {
  const res = spawnSync(process.execPath, [
    ...preloads.flatMap((p) => ['--require', p]), '--require', `${ADOT}/register`, '-e', PROBE,
  ], { cwd: app, encoding: 'utf8', timeout: 30_000, env: {
    PATH: process.env.PATH, HOME: process.env.HOME, AGENT_OBSERVABILITY_ENABLED: 'true',
    OTEL_METRICS_EXPORTER: 'none', AWS_ACCESS_KEY_ID: 'AKIDEXAMPLE', AWS_SECRET_ACCESS_KEY: 'x',
    ...env } });
  const line = (prefix) => (res.stdout.split('\n').find((l) => l.startsWith(prefix)) || '');
  return { res, hook: line('launchpad-adot-cn: '), probe: JSON.parse(line('PROBE ').slice(6) || '[]'),
    env: JSON.parse(line('ENV ').slice(4) || '{}') };
}

assert.ok(VERSIONS.length >= 2, 'the matrix lists the supported ADOT Node versions');
assert.ok(fs.existsSync(path.join(HERE, 'node_modules')),
  'backend/app/deployer/adot_cn: run `npm ci` first (the ADOT Node matrix is lockfile-pinned)');

for (const alias of VERSIONS) {
  test(`${alias}: the internals the hook uses exist`, () => {
    const root = path.join(HERE, 'node_modules', alias, 'build', 'src');
    const configurator = require(path.join(root, 'aws-opentelemetry-configurator.js'));
    assert.equal(typeof configurator.isAwsOtlpEndpoint, 'function', 'isAwsOtlpEndpoint moved');
    const { OTLPAwsSpanExporter } = require(
      path.join(root, 'exporter', 'otlp', 'aws', 'traces', 'otlp-aws-span-exporter.js'));
    assert.equal(typeof OTLPAwsSpanExporter, 'function', 'OTLPAwsSpanExporter moved');
    assert.equal(configurator.isAwsOtlpEndpoint(
      'https://xray.cn-northwest-1.amazonaws.com.cn/v1/traces', 'xray'), false,
    `${alias} accepts .cn natively — the hook reports "native"; drop the version from the matrix`);
  });

  test(`${alias}: in aws-cn the hook attaches ADOT's SigV4 exporter on the .cn endpoint`, () => {
    const out = run(appWith(alias), { AWS_REGION: 'cn-northwest-1' });
    assert.match(out.hook, /^launchpad-adot-cn: attached \(1 exporting span processor -> https:\/\/xray\.cn-northwest-1\.amazonaws\.com\.cn\/v1\/traces\)$/, out.res.stdout + out.res.stderr);
    assert.ok(out.probe.length >= 1);
    for (const exporter of out.probe) {
      assert.deepEqual(exporter, { cls: 'OTLPAwsSpanExporter', region: 'cn-northwest-1',
        url: 'https://xray.cn-northwest-1.amazonaws.com.cn/v1/traces' });
    }
    assert.equal(out.env.traces, 'https://xray.cn-northwest-1.amazonaws.com.cn/v1/traces');
    assert.equal(out.env.logs, 'none');
  });

  test(`${alias}: outside aws-cn the hook does nothing`, () => {
    const out = run(appWith(alias), { AWS_REGION: 'us-west-2' });
    assert.equal(out.hook, 'launchpad-adot-cn: skipped (region us-west-2 is not aws-cn)');
    assert.notEqual(out.env.logs, 'none');
  });

  test(`${alias}: a release that signs .cn itself switches the hook off`, () => {
    const stub = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'adot-cn-native-')), 'native.cjs');
    fs.writeFileSync(stub, `const p = require('node:path');
const c = require(p.join(p.dirname(require.resolve('${ADOT}/register', { paths: [process.cwd()] })), 'aws-opentelemetry-configurator.js'));
c.isAwsOtlpEndpoint = () => true;`);
    const out = run(appWith(alias), { AWS_REGION: 'cn-northwest-1' }, [stub, HOOK]);
    assert.equal(out.hook, 'launchpad-adot-cn: native (ADOT signs the aws-cn endpoint itself)');
  });
}

for (const alias of VERSIONS) {
  test(`${alias}: a moved internal fails open — the app still answers, the hook says unavailable`, () => {
    const stub = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'adot-cn-moved-')), 'moved.cjs');
    fs.writeFileSync(stub, `const p = require('node:path');
const root = p.dirname(require.resolve('${ADOT}/register', { paths: [process.cwd()] }));
// a release that renames the export the hook feature-detects with (ADOT's own calls are
// module-local, so ADOT itself keeps working)
require(p.join(root, 'aws-opentelemetry-configurator.js')).isAwsOtlpEndpoint = undefined;`);
    const res = spawnSync(process.execPath, ['--require', stub, '--require', HOOK, '--require',
      `${ADOT}/register`, '-e', 'setTimeout(() => console.log("app up"), 200)'], {
      cwd: appWith(alias), encoding: 'utf8', timeout: 30_000, env: {
        PATH: process.env.PATH, AWS_REGION: 'cn-northwest-1', AGENT_OBSERVABILITY_ENABLED: 'true',
        OTEL_METRICS_EXPORTER: 'none' } });
    assert.equal(res.status, 0, res.stderr);
    const lines = res.stdout.split('\n');
    assert.ok(lines.includes('launchpad-adot-cn: unavailable (ADOT internals changed: isAwsOtlpEndpoint)'), res.stdout);
    assert.ok(lines.includes('app up'), res.stdout);
  });
}

for (const alias of VERSIONS) {
  for (const value of ['none', 'console', 'zipkin']) {
    test(`${alias}: the app's OTEL_TRACES_EXPORTER=${value} wins — the hook touches nothing`, () => {
      const out = run(appWith(alias), { AWS_REGION: 'cn-northwest-1', OTEL_TRACES_EXPORTER: value });
      assert.equal(out.hook, `launchpad-adot-cn: skipped (traces disabled by the app: OTEL_TRACES_EXPORTER=${value})`);
      for (const exporter of out.probe) assert.notEqual(exporter.url, 'https://xray.cn-northwest-1.amazonaws.com.cn/v1/traces');
      assert.notEqual(out.env.logs, 'none');
    });
  }

  test(`${alias}: the app's OTEL_SDK_DISABLED=true wins too`, () => {
    const out = run(appWith(alias), { AWS_REGION: 'cn-northwest-1', OTEL_SDK_DISABLED: 'true' });
    assert.equal(out.hook, 'launchpad-adot-cn: skipped (the OpenTelemetry SDK is disabled by the app: OTEL_SDK_DISABLED=true)');
  });

  test(`${alias}: "attached" counts only ADOT's exporting (Batch/Simple + OTLP) processors`, () => {
    // the unsampled-span processor alone never exports a span of an always-on sampler
    const stub = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'adot-cn-unsampled-')), 'drop.cjs');
    fs.writeFileSync(stub, `setImmediate(() => {
  const api = require(require.resolve('@opentelemetry/api', { paths: [require('node:path').dirname(require.resolve('${ADOT}/register', { paths: [process.cwd()] }))] }));
  const multi = api.trace.getTracerProvider().getDelegate()._activeSpanProcessor;
  multi._spanProcessors = multi._spanProcessors.filter((p) => p.constructor.name !== 'BatchSpanProcessor');
});`);
    const out = run(appWith(alias), { AWS_REGION: 'cn-northwest-1' }, [stub, HOOK]);
    assert.match(out.hook, /^launchpad-adot-cn: unavailable \(no exporting span processor: AwsBatchUnsampledSpanProcessor\/OTLPAwsSpanExporter\)$/, out.res.stdout);
  });
}

test('an app without ADOT starts normally and says so', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adot-cn-none-'));
  const res = spawnSync(process.execPath, ['--require', HOOK, '-e', 'console.log("app up")'],
    { cwd: dir, encoding: 'utf8', env: { PATH: process.env.PATH, AWS_REGION: 'cn-northwest-1' } });
  assert.equal(res.status, 0);
  assert.deepEqual(res.stdout.trim().split('\n'), ['launchpad-adot-cn: skipped (no ADOT Node in the app)', 'app up']);
});
