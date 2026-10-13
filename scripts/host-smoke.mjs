// Run with: node scripts/host-smoke.mjs /absolute/path/to/xangi/dist
// Loads the real host implementation in an isolated process and temporary registry.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, rm, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const state = await mkdtemp(join(tmpdir(), 'xangi-dots-host-'));
process.env.XANGI_EXTENSIONS_FILE = join(state, 'extensions.json');
const host = await import(pathToFileURL(resolve(process.argv[2], 'extensions.js')).href);
const { executeExtensionRequest } = await import(pathToFileURL(resolve(process.argv[2], 'extension-request.js')).href);
let linked;
try {
  linked = await host.linkExtension(join(root, 'xangi-extension.json'), { autostart: false });
  const start = await host.runExtensionAction(linked, 'start', { workspace: state });
  assert.equal(start.ok, true, JSON.stringify(start));
  const request = async (path, method = 'GET', body) => JSON.parse(await executeExtensionRequest({
    id: 'xangi-dots', capability: 'dots.jobs', path, method,
    ...(body ? { 'body-json': JSON.stringify(body) } : {}),
  }));
  assert.equal((await request('/health')).ready, true);
  const binary = Buffer.alloc(1024 * 1024);
  for (let i = 0; i < binary.length; i++) binary[i] = i % 256;
  const digest = createHash('sha256').update(binary).digest('hex');
  const input = await request('/files', 'POST', {
    upload_key: 'host-input', name: 'input.bin', mime_type: 'application/octet-stream',
    size: binary.length, sha256: digest,
  });
  for (let offset = 0; offset < binary.length; offset += 49152) {
    await request(`/files/${input.id}/chunks`, 'POST', {
      offset, encoding: 'base64', data: binary.subarray(offset, offset + 49152).toString('base64'),
    });
  }
  await request(`/files/${input.id}/finish`, 'POST', {});
  const job = await request('/jobs', 'POST', {
    request_key: 'host-smoke', prompt: 'Compute 17 * 23',
    origin: { platform: 'discord', channel: 'isolated-test-no-delivery' },
    attachments: [input.id],
  });
  let connection = await request('/connection');
  assert.equal(connection.notifications_enabled, false);
  const call = async (name, args) => {
    const response = await fetch(`http://127.0.0.1:${connection.mcp_port}${connection.mcp_path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }),
    });
    const reply = await response.json();
    assert.ok(!reply.error && !reply.result.isError, JSON.stringify(reply));
    return reply.result.structuredContent;
  };
  assert.equal((await call('list_jobs', {})).jobs[0].id, job.id);
  const claim = await call('claim_job', { job_id: job.id, claim_key: 'host-smoke-worker' });
  const scope = { job_id: job.id, claim_token: claim.claim_token };
  const output = await call('create_file', {
    ...scope, upload_key: 'host-output', name: 'output.bin', mime_type: 'application/octet-stream',
    size: binary.length, sha256: digest,
  });
  for (let offset = 0; offset < binary.length; offset += 49152) {
    const chunk = await call('read_file', { ...scope, file_id: input.id, offset });
    assert.equal(chunk.data_base64, binary.subarray(offset, offset + 49152).toString('base64'));
    await call('write_file', { ...scope, file_id: output.id, offset, encoding: 'base64', data: chunk.data_base64 });
  }
  await call('finish_file', { ...scope, file_id: output.id });
  await call('submit_result', { job_id: job.id, claim_token: claim.claim_token, result: '391', outcome: 'completed' });
  const saved = await request(`/jobs/${job.id}`);
  assert.equal(saved.result, '391');
  assert.equal(saved.notification.status, 'pending');
  const restart = await host.runExtensionAction(linked, 'restart', { workspace: state });
  assert.equal(restart.ok, true, JSON.stringify(restart));
  assert.equal((await request(`/jobs/${job.id}`)).result, '391');
  const chunks = [];
  for (let offset = 0; offset < binary.length; offset += 49152) {
    const chunk = await request(`/files/${output.id}?offset=${offset}`);
    chunks.push(Buffer.from(chunk.data_base64, 'base64'));
  }
  assert.deepEqual(Buffer.concat(chunks), binary);
  // Native input/output uses paths and URLs, not base64 in the model context.
  const configPath = join(connection.state_directory, 'config.json');
  const config = JSON.parse(await readFile(configPath, 'utf8'));
  config.public_base_url = 'https://native-test.example';
  await writeFile(configPath, JSON.stringify(config));
  assert.equal((await host.runExtensionAction(linked, 'restart', { workspace: state })).ok, true);
  connection = await request('/connection');
  const nativeBytes = Buffer.alloc(5 * 1024 * 1024, 17);
  const sourcePath = join(state, 'native-input.bin');
  await writeFile(sourcePath, nativeBytes);
  const nativeInput = await request('/files/import-local', 'POST', {
    path: sourcePath, upload_key: 'native-input', mime_type: 'application/octet-stream',
  });
  const nativeJob = await request('/jobs', 'POST', {
    request_key: 'native-host-smoke', prompt: 'Native transfer test',
    origin: { platform: 'discord', channel: 'isolated-test-no-delivery' },
    attachments: [nativeInput.id],
  });
  const nativeClaim = await call('claim_job', { job_id: nativeJob.id, claim_key: 'native-worker' });
  const nativeScope = { job_id: nativeJob.id, claim_token: nativeClaim.claim_token };
  const link = await call('get_file_link', { ...nativeScope, file_id: nativeInput.id });
  const path = new URL(link.download_url);
  const nativeResponse = await fetch(`http://127.0.0.1:${connection.mcp_port}${path.pathname}${path.search}`);
  assert.equal(nativeResponse.status, 200);
  assert.deepEqual(Buffer.from(await nativeResponse.arrayBuffer()), nativeBytes);
  const exportPath = join(state, 'native-export.bin');
  await request(`/files/${nativeInput.id}/export-local`, 'POST', { path: exportPath });
  assert.deepEqual(await readFile(exportPath), nativeBytes);
  let nativeCdnVerified = false;
  // Optional private file reference from a real Dots test. Never print its signed URL.
  if (process.argv[3]) {
    const file = JSON.parse(await readFile(process.argv[3], 'utf8'));
    const received = await call('receive_file', {
      ...nativeScope, upload_key: 'native-received', name: 'returned.zip', mime_type: 'application/zip', file,
    });
    const receivedPath = join(state, 'native-received.zip');
    const exported = await request(`/files/${received.id}/export-local`, 'POST', { path: receivedPath });
    assert.equal(exported.sha256, received.sha256);
    assert.equal(createHash('sha256').update(await readFile(receivedPath)).digest('hex'), received.sha256);
    nativeCdnVerified = true;
  }
  await call('submit_result', { ...nativeScope, result: 'native transfer verified', outcome: 'completed' });
  // Simulate only the xangi trigger CLI; exercise the real host lifecycle and HTTP APIs.
  const triggerLog = join(state, 'trigger-argv.jsonl');
  const cliStub = join(state, 'trigger-stub');
  await writeFile(cliStub, '#!/usr/bin/env node\n' +
    `const fs = require('node:fs'); const a = process.argv.slice(2);\n` +
    `fs.appendFileSync(${JSON.stringify(triggerLog)}, JSON.stringify(a) + '\\n');\n` +
    `if (a[1] === 'trigger') console.log('Triggered (id: isolated-trigger, source: test)');\n` +
    `else console.log(JSON.stringify({triggerId:'isolated-trigger',status:'delivered'}));\n`, { mode: 0o700 });
  config.xangi_cli = cliStub;
  config.request_destination = { platform: 'discord', channel: 'isolated-inbox' };
  await writeFile(configPath, JSON.stringify(config));
  assert.equal((await host.runExtensionAction(linked, 'restart', { workspace: state })).ok, true);
  connection = await request('/connection');
  assert.equal(connection.parent_requests_enabled, true);
  const reverse = await call('request_xangi', {request_key:'reverse-test',prompt:'Compute 17 * 23',timeout_seconds:3600});
  assert.equal((await call('request_xangi', {request_key:'reverse-test',prompt:'Compute 17 * 23',timeout_seconds:3600})).id, reverse.id);
  let reverseStatus;
  for (let i = 0; i < 50; i++) {
    reverseStatus = await request(`/requests/${reverse.id}`);
    if (reverseStatus.dispatch === 'accepted') break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.equal(reverseStatus.dispatch, 'accepted');
  const parentClaim = await request(`/requests/${reverse.id}/claim`, 'POST', {claim_key:'parent-test'});
  const reverseOutput = await request(`/requests/${reverse.id}/files/import-local`, 'POST', {
    claim_token:parentClaim.claim_token, path:sourcePath, upload_key:'reverse-output', mime_type:'application/octet-stream',
  });
  await request(`/requests/${reverse.id}/result`, 'POST', {claim_token:parentClaim.claim_token,result:'391',outcome:'completed'});
  assert.equal((await call('get_xangi_request', {request_id:reverse.id})).result, '391');
  assert.equal((await host.runExtensionAction(linked, 'restart', { workspace: state })).ok, true);
  connection = await request('/connection');
  assert.equal((await call('get_xangi_request', {request_id:reverse.id})).status, 'completed');
  const reverseLink = await call('get_xangi_file_link', {request_id:reverse.id,file_id:reverseOutput.id});
  const reverseUrl = new URL(reverseLink.download_url);
  const reverseResponse = await fetch(`http://127.0.0.1:${connection.mcp_port}${reverseUrl.pathname}${reverseUrl.search}`);
  assert.equal(reverseResponse.status, 200);
  assert.deepEqual(Buffer.from(await reverseResponse.arrayBuffer()), nativeBytes);
  const calls = (await readFile(triggerLog, 'utf8')).trim().split('\n').map(JSON.parse)
    .filter(a => a.includes('xangi-dots-request:' + reverse.id));
  assert.equal(calls.length, 1);
  assert.equal(calls[0][calls[0].indexOf('--channel') + 1], 'isolated-inbox');
  assert.ok(!calls[0].join(' ').includes('Compute 17 * 23'));
  const doctor = await host.runExtensionAction(linked, 'doctor', { workspace: state });
  assert.equal(doctor.ok, true, JSON.stringify(doctor));
  console.log(JSON.stringify({ passed: true, checks: ['real-host-start', 'extension_request', 'MCP-roundtrip', '1MiB-binary-both-directions', '5MiB-native-input-export', 'restart-persistence', 'Dots-to-xangi-request-result', 'reverse-5MiB-output-HTTP', 'fixed-destination-single-dispatch', 'doctor'], nativeCdnVerified, externalDelivery: false }));
} finally {
  if (linked) await host.runExtensionAction(linked, 'stop');
  await rm(state, { recursive: true, force: true });
}
