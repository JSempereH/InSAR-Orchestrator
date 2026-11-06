import { useEffect, useState } from "react";

/** useState that survives reloads through localStorage (silently falls back to memory only). */
export function usePersistentState<T>(key: string, initial: T): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      if (raw) return { ...initial, ...JSON.parse(raw) } as T;
    } catch {
      /* storage unavailable or corrupted: use the default */
    }
    return initial;
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* ignore */
    }
  }, [key, value]);
  return [value, setValue];
}
