export type AuthRole = 'super_admin' | 'admin' | 'user';
export type AuthUser = { id: number; username: string; role: AuthRole };
