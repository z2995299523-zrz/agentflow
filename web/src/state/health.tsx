/** 服务状态（/health）：页头显示资料目录、执行层、模型；各页面据此提示没导入元数据、没配模型 Key */
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'

import { api, errorText } from '../api/client'
import type { Health } from '../api/types'

interface HealthState {
  health: Health | null
  error: string
  reload: () => void
}

const HealthContext = createContext<HealthState>({ health: null, error: '', reload: () => {} })

export function HealthProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState('')

  const reload = useCallback(() => {
    api.health()
      .then((value) => {
        setHealth(value)
        setError('')
      })
      .catch((reason: unknown) => setError(errorText(reason)))
  }, [])

  useEffect(reload, [reload])

  return <HealthContext.Provider value={{ health, error, reload }}>{children}</HealthContext.Provider>
}

export function useHealth(): HealthState {
  return useContext(HealthContext)
}
