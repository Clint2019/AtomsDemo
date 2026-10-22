import { useCallback, useEffect, useRef, useState } from "react";

export interface ResourceState<T> {
  data: T | null; loading: boolean; error: string | null;
  refresh: () => void; replace: (value: T) => void;
}

export function useResource<T>(key: string, load: () => Promise<T>): ResourceState<T> {
  const [nonce, setNonce] = useState(0);
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const refresh = useCallback(() => setNonce((value) => value + 1), []);
  const replace = useCallback((value: T) => {
    generation.current += 1;
    setData(value);
    setLoading(false);
    setError(null);
  }, []);
  useEffect(() => {
    let active = true;
    const current = ++generation.current;
    setLoading(true);
    setError(null);
    load().then((value) => { if (active && generation.current === current) setData(value); })
      .catch((cause: unknown) => {
        if (active && generation.current === current)
          setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => { if (active && generation.current === current) setLoading(false); });
    return () => { active = false; };
    // The key is the semantic resource identity; callers change it when inputs change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce]);
  return { data, loading, error, refresh, replace };
}
