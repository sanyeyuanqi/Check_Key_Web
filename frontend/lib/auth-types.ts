export type AuthRole = 'admin' | 'user';
export type AuthUser = { id: number; username: string; role: AuthRole };
