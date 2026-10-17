import LoginForm from './login-form';
import './login.css';

export const dynamic = 'force-dynamic';

export default function LoginPage() {
  return (
    <main className="login-page">
      <section aria-labelledby="login-title" className="login-card">
        <LoginForm />
      </section>
    </main>
  );
}
