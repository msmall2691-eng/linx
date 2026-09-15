import { createContext, useContext, useEffect, useMemo, useState } from 'react'

import { apiFetch } from './api.js'

// One region at launch, so its name and timezone come from the server rather
// than being hardcoded in the bundle — but there is deliberately no region
// *selector* anywhere in this app.

const FALLBACK = {
  region_name: 'your area',
  region_timezone: 'America/New_York',
  // Only reached if the config fetch fails. It matches the server's
  // `MAX_BULK_JOBS`, and being wrong here is safe in one direction only — the
  // server still refuses, so the cost is a form that offers less than it could
  // rather than one that offers more than the API will take.
  max_bulk_jobs: 100,
}

const ConfigContext = createContext(FALLBACK)

export function ConfigProvider({ children }) {
  const [config, setConfig] = useState(FALLBACK)

  useEffect(() => {
    let cancelled = false
    apiFetch('/config', { auth: false })
      .then((loaded) => {
        if (!cancelled) setConfig(loaded)
      })
      .catch(() => {
        // Keep the fallback. A failed config fetch should not blank out every
        // date on the page.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const value = useMemo(() => config, [config])
  return <ConfigContext.Provider value={value}>{children}</ConfigContext.Provider>
}

export function useConfig() {
  return useContext(ConfigContext)
}

/** The most jobs one bulk submission may create, as the server counts them. */
export function useMaxBulkJobs() {
  return useConfig().max_bulk_jobs ?? FALLBACK.max_bulk_jobs
}

/** The region timezone, which is what every date in this app is read in. */
export function useTimeZone() {
  return useConfig().region_timezone ?? FALLBACK.region_timezone
}
