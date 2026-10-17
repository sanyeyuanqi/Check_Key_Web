'use client';

import { useRef, useState } from 'react';
import { apiFetch } from '@/lib/api-client';

const translations = {
  zh: {
    title: '密钥管理系统',
    language: '语言',
    username: '用户名',
    password: '密码',
    usernamePlaceholder: '用户名',
    passwordPlaceholder: '密码',
    showPassword: '显示密码',
    hidePassword: '隐藏密码',
    submit: '登 录',
    submitting: '正在登录…',
    loginError: '登录失败，请稍后重试',
    networkError: '网络异常，请稍后重试',
  },
  en: {
    title: 'API Key Management System',
    language: 'Language',
    username: 'Username',
    password: 'Password',
    usernamePlaceholder: 'Username',
    passwordPlaceholder: 'Password',
    showPassword: 'Show password',
    hidePassword: 'Hide password',
    submit: 'Login',
    submitting: 'Logging in…',
    loginError: 'Login failed. Please try again later.',
    networkError: 'Network error. Please try again later.',
  },
} as const;

export default function LoginForm() {
  const [language, setLanguage] = useState<'zh' | 'en'>('zh');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const inFlight = useRef(false);
  const text = translations[language];

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    try {
      const response = await apiFetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const body = await response.json() as { error?: string };
      if (!response.ok) return setError(body.error || text.loginError);
      window.location.replace('/');
    } catch {
      setError(text.networkError);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <div lang={language === 'zh' ? 'zh-CN' : 'en'}>
      <div className="login-language-row">
        <div className="login-language-switch" role="group" aria-label={text.language}>
          <button type="button" disabled={busy} aria-pressed={language === 'zh'} onClick={() => setLanguage('zh')}>中文</button>
          <button type="button" disabled={busy} aria-pressed={language === 'en'} onClick={() => setLanguage('en')}>English</button>
        </div>
      </div>
      <h1 id="login-title" className="login-title">{text.title}</h1>
      <form onSubmit={submit} aria-busy={busy} className="login-form">
        <div className="login-field">
          <label htmlFor="login-username" className="login-sr-only">{text.username}</label>
          <span className="login-field-icon" aria-hidden="true">
            <svg width="16" height="16" viewBox="64 64 896 896" fill="currentColor" focusable="false">
              <path d="M858.5 763.6a374 374 0 00-80.6-119.5 375.63 375.63 0 00-119.5-80.6c-.4-.2-.8-.3-1.2-.5C719.5 518 760 444.7 760 362c0-137-111-248-248-248S264 225 264 362c0 82.7 40.5 156 102.8 201.1-.4.2-.8.3-1.2.5-44.8 18.9-85 46-119.5 80.6a375.63 375.63 0 00-80.6 119.5A371.7 371.7 0 00136 901.8a8 8 0 008 8.2h60c4.4 0 7.9-3.5 8-7.8 2-77.2 33-149.5 87.8-204.3 56.7-56.7 132-87.9 212.2-87.9s155.5 31.2 212.2 87.9C779 752.7 810 825 812 902.2c.1 4.4 3.6 7.8 8 7.8h60a8 8 0 008-8.2c-1-47.8-10.9-94.3-29.5-138.2zM512 534c-45.9 0-89.1-17.9-121.6-50.4S340 407.9 340 362c0-45.9 17.9-89.1 50.4-121.6S466.1 190 512 190s89.1 17.9 121.6 50.4S684 316.1 684 362c0 45.9-17.9 89.1-50.4 121.6S557.9 534 512 534z" />
            </svg>
          </span>
          <input id="login-username" name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} required maxLength={32} disabled={busy} value={username} onChange={(event) => setUsername(event.target.value)} aria-describedby={error ? 'login-error' : undefined} className="login-input" placeholder={text.usernamePlaceholder} />
        </div>
        <div className="login-field">
          <label htmlFor="login-password" className="login-sr-only">{text.password}</label>
          <span className="login-field-icon" aria-hidden="true">
            <svg width="16" height="16" viewBox="64 64 896 896" fill="currentColor" focusable="false">
              <path d="M832 464h-68V240c0-70.7-57.3-128-128-128H388c-70.7 0-128 57.3-128 128v224h-68c-17.7 0-32 14.3-32 32v384c0 17.7 14.3 32 32 32h640c17.7 0 32-14.3 32-32V496c0-17.7-14.3-32-32-32zM332 240c0-30.9 25.1-56 56-56h248c30.9 0 56 25.1 56 56v224H332V240zm460 600H232V536h560v304zM484 701v53c0 4.4 3.6 8 8 8h40c4.4 0 8-3.6 8-8v-53a48.01 48.01 0 10-56 0z" />
            </svg>
          </span>
          <input id="login-password" name="password" type={showPassword ? 'text' : 'password'} autoComplete="current-password" required minLength={8} maxLength={256} disabled={busy} value={password} onChange={(event) => setPassword(event.target.value)} aria-describedby={error ? 'login-error' : undefined} className="login-input" placeholder={text.passwordPlaceholder} />
          <button type="button" disabled={busy} aria-label={showPassword ? text.hidePassword : text.showPassword} aria-pressed={showPassword} aria-controls="login-password" onClick={() => setShowPassword((value) => !value)} className="login-password-toggle">
            {showPassword ? (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true" focusable="false">
                <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
            ) : (
              <svg width="16" height="16" viewBox="64 64 896 896" fill="currentColor" aria-hidden="true" focusable="false">
                <path d="M942.2 486.2Q889.47 375.11 816.7 305l-50.88 50.88C807.31 395.53 843.45 447.4 874.7 512 791.5 684.2 673.4 766 512 766q-72.67 0-133.87-22.38L323 798.75Q408 838 512 838q288.3 0 430.2-300.3a60.29 60.29 0 000-51.5zm-63.57-320.64L836 122.88a8 8 0 00-11.32 0L715.31 232.2Q624.86 186 512 186q-288.3 0-430.2 300.3a60.3 60.3 0 000 51.5q56.69 119.4 136.5 191.41L112.48 835a8 8 0 000 11.31L155.17 889a8 8 0 0011.31 0l712.15-712.12a8 8 0 000-11.32zM149.3 512C232.6 339.8 350.7 258 512 258c54.54 0 104.13 9.36 149.12 28.39l-70.3 70.3a176 176 0 00-238.13 238.13l-83.42 83.42C223.1 637.49 183.3 582.28 149.3 512zm246.7 0a112.11 112.11 0 01146.2-106.69L401.31 546.2A112 112 0 01396 512z" />
                <path d="M508 624c-3.46 0-6.87-.16-10.25-.47l-52.82 52.82a176.09 176.09 0 00227.42-227.42l-52.82 52.82c.31 3.38.47 6.79.47 10.25a111.94 111.94 0 01-112 112z" />
              </svg>
            )}
          </button>
        </div>
        {error && <p id="login-error" role="alert" className="login-error">{error}</p>}
        <button type="submit" disabled={busy} className="login-submit">{busy ? text.submitting : text.submit}</button>
      </form>
    </div>
  );
}
