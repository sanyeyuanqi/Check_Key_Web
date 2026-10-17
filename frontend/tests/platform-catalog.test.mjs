import assert from 'node:assert/strict';
import { test } from 'node:test';
import { platformOptions, platformByProvider, platformBySlug } from '../lib/platforms.ts';
import { credentialStorageTypes, inferStorageType, resultTableName, storageConfig } from '../lib/credential-types.ts';

test('Cloudflare precedes OpenCode while the existing navigation order and unique routes are preserved', () => {
  assert.deepEqual(platformOptions.map((platform) => platform.slug), [
    'anthropic', 'aws', 'azure', 'openai', 'google', 'openrouter', 'tokenrouter', 'vercel', 'cloudflare', 'opencode',
  ]);
  assert.equal(new Set(platformOptions.map((platform) => platform.slug)).size, platformOptions.length);
  assert.equal(new Set(platformOptions.map((platform) => platform.provider)).size, platformOptions.length);
  const platform = platformBySlug('opencode');
  assert.ok(platform);
  assert.equal(platform.provider, 'OpenCode');
  assert.equal(platform.title, 'OpenCode');
  assert.equal(platform.subtitle, 'Claude / Zen');
  assert.deepEqual(platformByProvider('OpenCode'), platform);
});

test('all navigation platforms have storage configurations with unique identities and tables', () => {
  for (const platform of platformOptions) {
    assert.ok(credentialStorageTypes.some((config) => config.provider === platform.provider), platform.provider);
  }
  assert.equal(new Set(credentialStorageTypes.map((config) => config.id)).size, credentialStorageTypes.length);
  assert.equal(new Set(credentialStorageTypes.map((config) => config.table)).size, credentialStorageTypes.length);
});

test('Cloudflare metadata and Workers AI storage use the canonical platform identity', () => {
  const platform = platformBySlug('cloudflare');
  assert.ok(platform);
  assert.equal(platform.provider, 'Cloudflare');
  assert.equal(platform.title, 'Cloudflare');
  assert.equal(platform.subtitle, 'Workers AI');
  assert.deepEqual(platformByProvider('Cloudflare'), platform);
  assert.deepEqual(storageConfig('cloudflare_workers_ai'), {
    id: 'cloudflare_workers_ai', table: 'cloudflare_workers_ai_keys', provider: 'Cloudflare', label: 'Cloudflare Workers AI',
  });
  assert.deepEqual(credentialStorageTypes.filter((config) => config.provider === 'Cloudflare'), [storageConfig('cloudflare_workers_ai')]);
});

test('Cloudflare provider wins over generic credential kinds, including Anthropic OAuth', () => {
  for (const kind of ['cloudflare', 'openai', 'anthropic', 'anthropic_oauth', 'synthetic-unknown-kind']) {
    assert.equal(inferStorageType('Cloudflare', kind, 'claude-synthetic'), 'cloudflare_workers_ai', kind);
  }
});

test('Cloudflare kind wins over generic provider classification', () => {
  for (const provider of ['OpenAI', 'Anthropic', 'Synthetic Provider']) {
    assert.equal(inferStorageType(provider, 'cloudflare', 'claude-synthetic'), 'cloudflare_workers_ai', provider);
  }
});

test('OpenCode uses the desktop canonical Zen storage identity', () => {
  assert.deepEqual(storageConfig('opencode_zen'), {
    id: 'opencode_zen', table: 'opencode_zen_keys', provider: 'OpenCode', label: 'OpenCode Zen',
  });
  assert.deepEqual(credentialStorageTypes.filter((config) => config.provider === 'OpenCode'), [storageConfig('opencode_zen')]);
  assert.equal(new Set(credentialStorageTypes.map((config) => config.id)).size, credentialStorageTypes.length);
});

test('OpenCode provider wins over generic credential kinds, including Anthropic OAuth', () => {
  for (const kind of ['opencode', 'openai', 'anthropic', 'anthropic_oauth', 'synthetic-unknown-kind']) {
    assert.equal(inferStorageType('OpenCode', kind, 'claude-synthetic'), 'opencode_zen', kind);
  }
});

test('OpenCode kind wins over generic provider classification', () => {
  for (const provider of ['OpenAI', 'Anthropic', 'Synthetic Provider']) {
    assert.equal(inferStorageType(provider, 'opencode', 'claude-synthetic'), 'opencode_zen', provider);
  }
});

test('existing provider and kind inference remains compatible', () => {
  const cases = [
    ['Anthropic', 'anthropic', null, 'anthropic_official'],
    ['Anthropic', 'anthropic_oauth', null, 'anthropic_enterprise_subscription'],
    ['Synthetic Provider', 'anthropic', null, 'anthropic_official'],
    ['AWS Bedrock', 'bedrock_api_key', null, 'aws_bedrock_api'],
    ['AWS Bedrock', 'aws_access_key', null, 'aws_access'],
    ['OpenAI', 'openai', null, 'openai_official'],
    ['Synthetic Provider', 'openai', null, 'openai_official'],
    ['OpenRouter', 'openrouter', null, 'openrouter'],
    ['TokenRouter', 'tokenrouter', null, 'tokenrouter'],
    ['Vercel', 'vercel', null, 'vercel'],
    ['Synthetic Provider', 'vercel', null, 'vercel'],
    ['Azure', 'azure', 'gpt-synthetic', 'azure_gpt'],
    ['Azure', 'azure', 'claude-synthetic', 'azure_claude'],
    ['Synthetic Provider', 'azure', 'CLAUDE-synthetic', 'azure_claude'],
    ['Azure', 'azure', null, 'azure_gpt'],
    ['Google', 'google_ai_studio', 'gemini-synthetic', 'google_ai_studio_gemini'],
    ['Google', 'google_vertex_service_account', 'gemini-synthetic', 'google_vertex_gemini'],
    ['Google', 'google_vertex_token', 'claude-synthetic', 'google_vertex_claude'],
    ['Synthetic Provider', 'google_vertex_token', 'CLAUDE-synthetic', 'google_vertex_claude'],
    ['Google', 'google_vertex_token', null, 'google_vertex_gemini'],
  ];
  for (const [provider, kind, model, expected] of cases) {
    assert.equal(inferStorageType(provider, kind, model), expected, `${provider} / ${kind} / ${model}`);
  }
});

test('all four OpenCode result buckets address the canonical Zen tables', () => {
  const config = storageConfig('opencode_zen');
  assert.ok(config);
  for (const [bucket, expected] of [
    ['valid', 'opencode_zen_valid_keys'],
    ['usage_limit_reached', 'opencode_zen_usage_limit_reached_keys'],
    ['insufficient_credit', 'opencode_zen_insufficient_credit_keys'],
    ['invalid', 'opencode_zen_invalid_keys'],
  ]) assert.equal(resultTableName(config, bucket), expected);
});

test('all four Cloudflare result buckets address the canonical Workers AI tables', () => {
  const config = storageConfig('cloudflare_workers_ai');
  assert.ok(config);
  for (const [bucket, expected] of [
    ['valid', 'cloudflare_workers_ai_valid_keys'],
    ['usage_limit_reached', 'cloudflare_workers_ai_usage_limit_reached_keys'],
    ['insufficient_credit', 'cloudflare_workers_ai_insufficient_credit_keys'],
    ['invalid', 'cloudflare_workers_ai_invalid_keys'],
  ]) assert.equal(resultTableName(config, bucket), expected);
});
