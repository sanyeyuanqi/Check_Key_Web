export const platformOptions = [
  { slug: 'anthropic', provider: 'Anthropic', title: 'Anthropic', subtitle: 'Claude 官方', mark: 'A', tone: 'platform-mark-anthropic' },
  { slug: 'aws', provider: 'AWS Bedrock', title: 'AWS', subtitle: 'Amazon Bedrock', mark: 'A', tone: 'platform-mark-aws' },
  { slug: 'azure', provider: 'Azure', title: 'Azure', subtitle: 'Microsoft Azure', mark: 'A', tone: 'platform-mark-azure' },
  { slug: 'openai', provider: 'OpenAI', title: 'OpenAI', subtitle: 'GPT 官方', mark: 'O', tone: 'platform-mark-openai' },
  { slug: 'google', provider: 'Google', title: 'Google', subtitle: 'Gemini / Vertex', mark: 'G', tone: 'platform-mark-google' },
  { slug: 'openrouter', provider: 'OpenRouter', title: 'OpenRouter', subtitle: 'Claude / K3', mark: 'O', tone: 'platform-mark-openrouter' },
  { slug: 'tokenrouter', provider: 'TokenRouter', title: 'TokenRouter', subtitle: 'Claude / Models', mark: 'T', tone: 'platform-mark-tokenrouter' },
  { slug: 'vercel', provider: 'Vercel', title: 'Vercel', subtitle: 'Claude / AI Gateway', mark: 'V', tone: 'platform-mark-vercel' },
  { slug: 'cloudflare', provider: 'Cloudflare', title: 'Cloudflare', subtitle: 'Workers AI', mark: 'C', tone: 'platform-mark-cloudflare' },
  { slug: 'opencode', provider: 'OpenCode', title: 'OpenCode', subtitle: 'Claude / Zen', mark: 'O', tone: 'platform-mark-opencode' },
] as const;

export type PlatformOption = (typeof platformOptions)[number];

export function platformBySlug(slug: string): PlatformOption | undefined {
  return platformOptions.find((item) => item.slug === slug);
}

export function platformByProvider(provider: string): PlatformOption | undefined {
  return platformOptions.find((item) => item.provider === provider);
}
