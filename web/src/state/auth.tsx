/** 在加载受保护页面前确认应用账号，切换用户时卸载旧会话。 */
import { Alert, Button, Card, Form, Input, Spin, Typography } from 'antd'
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import { api, errorText } from '../api/client'
import type { LoginState, UserProfile } from '../api/types'

const AuthContext = createContext<{ user: UserProfile | null; canManage: boolean; logout: () => Promise<void> }>({
  user: null, canManage: false, logout: async () => {},
})

export function useAuth() {
  return useContext(AuthContext)
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const [session, setSession] = useState<LoginState | null>(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [form] = Form.useForm<{ username: string; password: string }>()

  const load = useCallback(() => {
    setError('')
    void api.me().then(setSession).catch((reason: unknown) => setError(errorText(reason)))
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    const expired = () => {
      setSession({ required: true, user: null })
      setError('登录已失效，请重新登录。')
      navigate('/', { replace: true })
    }
    window.addEventListener('agentflow-session-expired', expired)
    return () => window.removeEventListener('agentflow-session-expired', expired)
  }, [navigate])

  const login = async ({ username, password }: { username: string; password: string }) => {
    setError('')
    setSubmitting(true)
    try {
      const state = await api.login(username, password)
      form.resetFields()
      setSession(state)
    } catch (reason: unknown) {
      setError(errorText(reason))
      form.setFieldValue('password', '')
    } finally { setSubmitting(false) }
  }

  const logout = async () => {
    await api.logout()
    setError('')
    setSession({ required: true, user: null })
    navigate('/', { replace: true })
  }

  if (!session && !error) return <div className="page-loading"><Spin /></div>
  if (!session || (session.required && !session.user)) return (
    <main className="sign-in-page">
      <Card className="sign-in-card">
        <img src="/favicon.svg" alt="" className="brand-logo" />
        <Typography.Title level={2}>数仓查数助手</Typography.Title>
        <Typography.Paragraph type="secondary">登录后使用已授权的业务数据。</Typography.Paragraph>
        {error && <Alert showIcon type="error" title={error} className="service-alert" />}
        {session ? <Form form={form} layout="vertical" onFinish={(value) => void login(value)}>
          <Form.Item label="登录账号" name="username" rules={[{ required: true, message: '请输入账号' }]}>
            <Input autoComplete="username" maxLength={40} size="large" />
          </Form.Item>
          <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password autoComplete="current-password" maxLength={128} size="large" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={submitting} size="large" block>登录</Button>
        </Form> : <Button onClick={load}>重试</Button>}
      </Card>
    </main>
  )

  return <AuthContext.Provider key={session.user?.id ?? 'single-user'} value={{
    user: session.user, canManage: !session.required || Boolean(session.user?.admin), logout,
  }}>{children}</AuthContext.Provider>
}
