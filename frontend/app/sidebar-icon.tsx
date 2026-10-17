import Image from 'next/image';
import type { PlatformOption } from '@/lib/platforms';

const brandAssets: Record<PlatformOption['slug'], string> = {
  openai: '/brands/openai.png',
  anthropic: '/brands/anthropic.png',
  google: '/brands/google.png',
  azure: '/brands/azure.svg',
  aws: '/brands/aws.svg',
  openrouter: '/brands/openrouter.svg',
  tokenrouter: '/brands/tokenrouter.svg',
  vercel: '/brands/vercel.svg',
  cloudflare: '/brands/cloudflare.svg',
  opencode: '/brands/opencode.svg',
};

export function SidebarBrandIcon({ slug }: { slug: PlatformOption['slug'] }) {
  return <span aria-hidden="true" className="sidebar-icon"><Image src={brandAssets[slug]} alt="" width={20} height={20} unoptimized className={`sidebar-brand-image sidebar-brand-image--${slug}`} /></span>;
}

export function SidebarUtilityIcon({ name }: { name: 'accounts' | 'export-records' | 'valid-key-records' }) {
  return <span aria-hidden="true" className={`sidebar-icon sidebar-utility-icon sidebar-utility-icon--${name}`} />;
}
