import { MutationCache, QueryClient } from "@tanstack/react-query";
import type { AxiosError } from "axios";

/** What the server said went wrong (its `detail`), or the error as text. */
export function errorMessage(error: unknown): string {
  const detail = (error as AxiosError<{ detail?: unknown }>)?.response?.data?.detail;
  return typeof detail === "string" ? detail : String(error);
}

/**
 * The query client of the app. Every mutation that fails without handling the failure itself
 * (deleting a project, cancelling a queue, ...) would otherwise do nothing visible, so
 * `report` is called with what went wrong.
 */
export function createQueryClient(report: (message: string) => void): QueryClient {
  return new QueryClient({
    mutationCache: new MutationCache({
      onError: (error, _variables, _context, mutation) => {
        if (mutation.options.onError) return;       // that mutation reports its own failure
        report(errorMessage(error));
      },
    }),
    defaultOptions: {
      queries: {
        // Without this, staleTime defaults to 0: every query refetches on every mount and window
        // focus, even ones that already have their own refetchInterval. Most data here (projects,
        // batches, credentials) does not change from second to second.
        staleTime: 10_000,
      },
    },
  });
}
