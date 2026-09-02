import LoginForm from './login-form';

export const dynamic = 'force-dynamic';

export default function LoginPage() {
  return (
    <main className="grid min-h-screen place-items-center bg-[#f5f7fb] px-5 py-10 text-slate-950">
      <section className="w-full max-w-md overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-[0_24px_70px_rgba(15,23,42,0.12)]">
        <div className="bg-gradient-to-br from-indigo-600 to-violet-600 px-8 py-8 text-white">
          <div className="grid h-12 w-12 place-items-center rounded-2xl bg-white/15 text-lg font-bold ring-1 ring-white/30">K</div>
          <h1 className="mt-6 text-2xl font-semibold tracking-tight">登录密钥管理中心</h1>
          <p className="mt-2 text-sm leading-6 text-indigo-100">本站不开放注册，账号由管理员统一创建。</p>
        </div>
        <LoginForm />
      </section>
    </main>
  );
}
