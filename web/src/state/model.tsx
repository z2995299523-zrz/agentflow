/**
 * 模型目录和默认模型来自 /nl2sql/models；每个会话的选择由会话状态管理。
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

import { api, errorText } from '../api/client'
import type { ModelInfo } from '../api/types'

interface ModelState {
  models: ModelInfo[] | null          // null：还没取到
  defaultModel: string | undefined
  error: string
  refresh: () => Promise<string | undefined>
  labelOf: (name: string | null | undefined) => string
}

const ModelContext = createContext<ModelState | null>(null)

export function ModelProvider({ children }: { children: ReactNode }) {
  const [models, setModels] = useState<ModelInfo[] | null>(null)
  const [defaultModel, setDefaultModel] = useState<string>()
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const value = await api.models()
      const selected = value.models.find((item) => item.name === value.default)?.name ?? value.models[0]?.name
      setModels(value.models)
      setDefaultModel(selected)
      setError('')
      return selected
    } catch (reason: unknown) {
      setError(errorText(reason))
      return undefined
    }
  }, [])
  useEffect(() => { void refresh() }, [refresh])

  const labelOf = useCallback((name: string | null | undefined) => {
    if (!name) return ''
    return models?.find((item) => item.name === name)?.label ?? name
  }, [models])

  const value = useMemo<ModelState>(() => ({ models, defaultModel, error, labelOf, refresh }),
                                    [models, defaultModel, error, labelOf, refresh])
  return <ModelContext.Provider value={value}>{children}</ModelContext.Provider>
}

export function useModel(): ModelState {
  const value = useContext(ModelContext)
  if (!value) throw new Error('useModel 要放在 ModelProvider 里面用')
  return value
}
