export const platformOptions = [
  { slug: 'aws', provider: 'AWS Bedrock', title: 'AWS', subtitle: 'Amazon Bedrock', mark: 'A', tone: 'bg-amber-500 shadow-amber-200' },
  { slug: 'anthropic', provider: 'Anthropic', title: 'Anthropic', subtitle: 'Claude 官方', mark: 'A', tone: 'bg-[#dc7d61] shadow-orange-200' },
  { slug: 'openai', provider: 'OpenAI', title: 'OpenAI', subtitle: 'GPT 官方', mark: 'O', tone: 'bg-emerald-600 shadow-emerald-200' },
  { slug: 'azure', provider: 'Azure', title: 'Azure', subtitle: 'Microsoft Azure', mark: 'A', tone: 'bg-sky-600 shadow-sky-200' },
  { slug: 'google', provider: 'Google', title: 'Google', subtitle: 'Gemini / Vertex', mark: 'G', tone: 'bg-blue-500 shadow-blue-200' },
  { slug: 'openrouter', provider: 'OpenRouter', title: 'OpenRouter', subtitle: 'Claude / K3', mark: 'O', tone: 'bg-violet-600 shadow-violet-200' },
] as const;

export type PlatformOption = (typeof platformOptions)[number];

export function platformBySlug(slug: string): PlatformOption | undefined {
  return platformOptions.find((item) => item.slug === slug);
}

export function platformByProvider(provider: string): PlatformOption | undefined {
  return platformOptions.find((item) => item.provider === provider);
}
