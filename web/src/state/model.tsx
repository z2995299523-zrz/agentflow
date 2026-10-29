/**
 * 查数用哪个模型：能选的模型来自 /nl2sql/models（只列配了 Key 的），选择记在本机浏览器里
 * 记下的模型不在列表里了（服务端撤了 Key）就退回默认模型；列表还没取到时不指定，服务端用默认模型。
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

import { api, errorText } from '../api/client'
import type { ModelInfo } from '../api/types'

const STORAGE_KEY = 'agentflow.model'

interface ModelState {
  models: ModelInfo[] | null          // null：还没取到
  model: string | undefined           // 查数时带上的模型；undefined 时服务端用默认模型
  error: string
  setModel: (name: string) => void
  labelOf: (name: string | null | undefined) => string
}

const ModelContext = createContext<ModelState | null>(null)

/** 读、写本机记下的模型：无痕窗口、禁用存储时会抛错，当作没记 */
function readStored(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

function writeStored(name: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, name)
  } catch {
    // 存不下就只在这次打开的页面里生效
  }
}

export function ModelProvider({ children }: { children: ReactNode }) {
  const [models, setModels] = useState<ModelInfo[] | null>(null)
  const [fallback, setFallback] = useState('')
  const [chosen, setChosen] = useState(readStored)
  const [error, setError] = useState('')

  useEffect(() => {
    api.models()
      .then((value) => {
        setModels(value.models)
        setFallback(value.default)
        setError('')
      })
      .catch((reason: unknown) => setError(errorText(reason)))
  }, [])

  const model = useMemo(() => {
    if (!models) return undefined
    const names = models.map((item) => item.name)
    if (names.includes(chosen)) return chosen
    return names.includes(fallback) ? fallback : names[0]
  }, [models, chosen, fallback])

  const setModel = useCallback((name: string) => {
    setChosen(name)
    writeStored(name)
  }, [])

  const labelOf = useCallback((name: string | null | undefined) => {
    if (!name) return ''
    return models?.find((item) => item.name === name)?.label ?? name
  }, [models])

  const value = useMemo<ModelState>(() => ({ models, model, error, setModel, labelOf }),
                                    [models, model, error, setModel, labelOf])
  return <ModelContext.Provider value={value}>{children}</ModelContext.Provider>
}

export function useModel(): ModelState {
  const value = useContext(ModelContext)
  if (!value) throw new Error('useModel 要放在 ModelProvider 里面用')
  return value
}
