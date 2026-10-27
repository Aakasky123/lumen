"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ChatThread } from "@/lib/api";

/**
 * The chat list for the sidebar and command menu. Refreshes when `signal` changes (a new thread,
 * a sent message) and every 10 s, so titles and ordering follow the backend.
 */
export function useThreads(signal: unknown) {
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [loaded, setLoaded] = useState(false);
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      setThreads(await api.chatThreads());
      setLoaded(true);
    } catch {
      // Keep the last list; the next tick retries.
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, signal]);

  useEffect(() => {
    const interval = window.setInterval(() => void refresh(), 10_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  const forget = useCallback((threadId: number) => {
    setThreads((current) => current.filter((thread) => thread.id !== threadId));
  }, []);

  return { threads, loaded, refresh, forget };
}
