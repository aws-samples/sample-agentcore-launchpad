// launchpad-adot-cn.cjs — N17-obs465 (user ruling 2026-10-26: the product maintains the aws-cn
// trace export for TypeScript agents). Preloaded by the platform-owned image layer through
// NODE_OPTIONS=--require; never edited per agent.
//
// ADOT Node (<= 0.13.0) builds `https://xray.<region>.amazonaws.com/v1/traces` and signs only
// endpoints matching `^https://xray\.<region>\.amazonaws\.com/v1/traces$`, so in aws-cn every
// export fails (N17-obs459). This hook, in an aws-cn region only, and only when the app left
// trace export on OTLP (an explicit OTEL_TRACES_EXPORTER without `otlp` — `none`, `console`, … —
// is the app's choice: the hook touches nothing and says so):
//   1. before ADOT starts: points the traces endpoint at the `.amazonaws.com.cn` host (when the
//      app set none) and turns the OTLP logs exporter off (when the app set none — ADOT would
//      send it unsigned; spans carry the conversation);
//   2. feature-detects: if ADOT's own `isAwsOtlpEndpoint` accepts that endpoint, ADOT signs it
//      natively and the hook does nothing ("native") — a fixed ADOT release disables it;
//   3. otherwise, once ADOT has installed its tracer provider, swaps the exporter of each of
//      ADOT's span processors for ADOT's own SigV4 `OTLPAwsSpanExporter` on that endpoint.
//      "attached" means EXPORTING: it counts only a Batch/Simple span processor whose exporter
//      was ADOT's OTLP one (the agent-observability unsampled-span processor alone exports no
//      span of an always-on sampler); with none of those it reports "unavailable (<found>)".
// It never throws and never blocks start-up; it prints ONE line the platform reads back:
//   launchpad-adot-cn: attached|native|skipped|unavailable (<reason>)
// The ADOT internals it touches are pinned per supported version by test/hook.test.cjs.
'use strict';

(function launchpadAdotCn() {
  const TAG = 'launchpad-adot-cn';
  let reported = false;
  const report = (state, reason) => {
    if (reported) return;
    reported = true;
    const text = String(reason || '').split('\n')[0].trim(); // one line, whatever the error
    console.log(`${TAG}: ${state}${text ? ` (${text})` : ''}`);
  };
  try {
    const region = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || '';
    if (!region.startsWith('cn-')) return report('skipped', `region ${region || 'unset'} is not aws-cn`);
    if ((process.env.OTEL_SDK_DISABLED || '').trim().toLowerCase() === 'true') {
      return report('skipped', 'the OpenTelemetry SDK is disabled by the app: OTEL_SDK_DISABLED=true');
    }
    const configured = (process.env.OTEL_TRACES_EXPORTER || '').trim();
    if (configured && !configured.split(',').map((s) => s.trim()).includes('otlp')) {
      return report('skipped', `traces disabled by the app: OTEL_TRACES_EXPORTER=${configured}`);
    }
    const path = require('node:path');
    let root;
    try {
      root = path.dirname(require.resolve(
        '@aws/aws-distro-opentelemetry-node-autoinstrumentation/register', { paths: [process.cwd()] }));
    } catch {
      return report('skipped', 'no ADOT Node in the app');
    }
    const endpoint = process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
      || `https://xray.${region}.amazonaws.com.cn/v1/traces`;
    if (!process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT && !process.env.OTEL_EXPORTER_OTLP_ENDPOINT) {
      process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = endpoint;
    }
    // the package's "exports" map hides these modules: resolve them next to register.js
    const configurator = require(path.join(root, 'aws-opentelemetry-configurator.js'));
    if (typeof configurator.isAwsOtlpEndpoint !== 'function') {
      return report('unavailable', 'ADOT internals changed: isAwsOtlpEndpoint');
    }
    if (configurator.isAwsOtlpEndpoint(endpoint.toLowerCase(), 'xray')) {
      return report('native', 'ADOT signs the aws-cn endpoint itself');
    }
    if (!process.env.OTEL_LOGS_EXPORTER) process.env.OTEL_LOGS_EXPORTER = 'none';
    const { OTLPAwsSpanExporter } = require(
      path.join(root, 'exporter', 'otlp', 'aws', 'traces', 'otlp-aws-span-exporter.js'));
    if (typeof OTLPAwsSpanExporter !== 'function') {
      return report('unavailable', 'ADOT internals changed: OTLPAwsSpanExporter');
    }
    const api = require(require.resolve('@opentelemetry/api', { paths: [root, process.cwd()] }));
    const deadline = Date.now() + 10_000;
    const attach = () => {
      try {
        const provider = api.trace.getTracerProvider();
        const delegate = typeof provider.getDelegate === 'function' ? provider.getDelegate() : provider;
        const processors = delegate && delegate._activeSpanProcessor
          && delegate._activeSpanProcessor._spanProcessors;
        if (Array.isArray(processors) && processors.length) {
          const name = (o) => (o && o.constructor && o.constructor.name) || '?';
          const found = [];
          let exporting = 0;
          for (const processor of processors) {
            if (!processor || !Object.prototype.hasOwnProperty.call(processor, '_exporter')) continue;
            const kind = `${name(processor)}/${name(processor._exporter)}`;
            found.push(kind);
            if (/^(BatchSpanProcessor|SimpleSpanProcessor)\/(OTLPTraceExporter|OTLPAwsSpanExporter)$/.test(kind)) {
              exporting += 1;
            }
            processor._exporter = new OTLPAwsSpanExporter(endpoint.toLowerCase());
          }
          if (exporting) {
            return report('attached', `${exporting} exporting span processor${exporting > 1 ? 's' : ''} -> ${endpoint}`);
          }
          return report('unavailable', `no exporting span processor: ${found.join(', ') || 'none'}`);
        }
      } catch (err) {
        return report('unavailable', `attach failed: ${err && err.message}`);
      }
      if (Date.now() > deadline) return report('unavailable', 'ADOT set no tracer provider within 10 s');
      const timer = setTimeout(attach, 50);
      if (timer && typeof timer.unref === 'function') timer.unref();
      return undefined;
    };
    setImmediate(attach); // NODE_OPTIONS preloads run before the app's own --require register
  } catch (err) {
    report('unavailable', `hook error: ${err && err.message}`);
  }
  return undefined;
})();
