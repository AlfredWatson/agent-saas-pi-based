import { createContext, useCallback, useContext, useEffect, useMemo, useState, type PropsWithChildren } from "react";
import { api, accessToken, clearToken, storeToken } from "./api";
import type { User } from "./types";

type AuthContextValue = {
  user: User | null;
  ready: boolean;
  signIn: (email: string, password: string, register?: boolean) => Promise<void>;
  signOut: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const signOut = useCallback(() => { clearToken(); setUser(null); }, []);
  useEffect(() => {
    if (!accessToken()) { setReady(true); return; }
    void api.me().then(setUser).catch(signOut).finally(() => setReady(true));
  }, [signOut]);
  useEffect(() => {
    window.addEventListener("pi-saas:unauthorized", signOut);
    return () => window.removeEventListener("pi-saas:unauthorized", signOut);
  }, [signOut]);
  const signIn = useCallback(async (email: string, password: string, register = false) => {
    const result = register ? await api.register(email, password) : await api.login(email, password);
    storeToken(result.access_token);
    setUser(await api.me());
  }, []);
  const value = useMemo(() => ({ user, ready, signIn, signOut }), [user, ready, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
