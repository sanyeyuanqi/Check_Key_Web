export const credentialStorageTypes = [
  { id: 'anthropic_official', table: 'anthropic_keys', provider: 'Anthropic', label: 'Anthropic 官Key' },
  { id: 'aws_bedrock_api', table: 'aws_bedrock_api_keys', provider: 'AWS Bedrock', label: 'Bedrock API Key' },
  { id: 'aws_access', table: 'aws_access_keys', provider: 'AWS Bedrock', label: 'AWS Access Key' },
  { id: 'openai_official', table: 'openai_keys', provider: 'OpenAI', label: 'OpenAI 官Key' },
  { id: 'openrouter', table: 'openrouter_keys', provider: 'OpenRouter', label: 'OpenRouter' },
  { id: 'azure_gpt', table: 'azure_gpt_keys', provider: 'Azure', label: 'AZ GPT' },
  { id: 'azure_claude', table: 'azure_claude_keys', provider: 'Azure', label: 'AZ Claude' },
  { id: 'google_ai_studio_gemini', table: 'google_ai_studio_gemini_keys', provider: 'Google', label: 'AI Studio · Gemini' },
  { id: 'google_vertex_gemini', table: 'google_vertex_gemini_keys', provider: 'Google', label: 'Vertex AI · Gemini' },
  { id: 'google_vertex_claude', table: 'google_vertex_claude_keys', provider: 'Google', label: 'Vertex AI · Claude' },
] as const;

export type CredentialStorageType = typeof credentialStorageTypes[number]['id'];
export type CredentialStorageConfig = typeof credentialStorageTypes[number];

export const credentialResultBuckets = [
  { id: 'valid', label: '可用', suffix: 'valid' },
  { id: 'usage_limit_reached', label: '额度待恢复', suffix: 'usage_limit_reached' },
  { id: 'insufficient_credit', label: '余额不足', suffix: 'insufficient_credit' },
  { id: 'invalid', label: '不可用', suffix: 'invalid' },
] as const;

export type CredentialResultBucket = typeof credentialResultBuckets[number]['id'];

export function resultBucket(status: string | null | undefined): CredentialResultBucket {
  if (status === 'valid' || status === 'usage_limit_reached' || status === 'insufficient_credit') return status;
  return 'invalid';
}

export function resultTableName(config: CredentialStorageConfig, bucket: CredentialResultBucket): string {
  const stem = config.table.endsWith('_keys') ? config.table.slice(0, -5) : config.table;
  const suffix = credentialResultBuckets.find((item) => item.id === bucket)!.suffix;
  return `${stem}_${suffix}_keys`;
}

export function storageConfig(value: string | null | undefined): CredentialStorageConfig | undefined {
  return credentialStorageTypes.find((item) => item.id === value);
}

export function inferStorageType(provider: string, kind: string, model?: string | null): CredentialStorageType {
  if (provider === 'Anthropic' || kind === 'anthropic') return 'anthropic_official';
  if (kind === 'bedrock_api_key') return 'aws_bedrock_api';
  if (kind === 'aws_access_key') return 'aws_access';
  if (provider === 'OpenAI' || kind === 'openai') return 'openai_official';
  if (provider === 'OpenRouter' || kind === 'openrouter') return 'openrouter';
  if (provider === 'Azure' || kind === 'azure') return model?.toLowerCase().startsWith('claude-') ? 'azure_claude' : 'azure_gpt';
  if (kind === 'google_ai_studio') return 'google_ai_studio_gemini';
  if (kind.startsWith('google_vertex_')) return model?.toLowerCase().startsWith('claude-') ? 'google_vertex_claude' : 'google_vertex_gemini';
  throw new Error('无法识别密钥存储类型');
}
