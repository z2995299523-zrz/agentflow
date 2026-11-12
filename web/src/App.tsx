/** 页面框架：页头（品牌、导航、服务状态）加两页（查数、元数据）。元数据页按需加载，血缘图的代码不进首屏 */
import { DatabaseOutlined, MessageOutlined, SettingOutlined, TeamOutlined } from '@ant-design/icons'
import { App as AntApp, Button, Dropdown, Menu, Spin } from 'antd'
import { lazy, Suspense } from 'react'
import { Link, Navigate, Route, Routes, useLocation } from 'react-router'

import StatusBar from './components/StatusBar'
import QueryPage from './pages/QueryPage'
import { ChatProvider, useChat } from './state/chat'
import { AuthProvider, useAuth } from './state/auth'
import { HealthProvider } from './state/health'
import { ModelProvider } from './state/model'

const MetadataPage = lazy(() => import('./pages/MetadataPage'))
const ModelSettingsPage = lazy(() => import('./pages/ModelSettingsPage'))
const UserAdministrationPage = lazy(() => import('./pages/UserAdministrationPage'))

function Header() {
  const location = useLocation()
  const { conversationId } = useChat()
  const { user, canManage, logout } = useAuth()
  const { message } = AntApp.useApp()
  const section = location.pathname.split('/')[1] || 'query'
  return (
    <header className="app-header">
      <Link to="/" className="brand">
        <img src="/favicon.svg" alt="" className="brand-logo" />
        <span className="brand-name">专题库查数副驾驶</span>
      </Link>
      <Menu
        mode="horizontal"
        className="app-menu"
        selectedKeys={['metadata', 'models', 'users'].includes(section) ? [section] : ['query']}
        items={[
          { key: 'query', icon: <MessageOutlined />,
            label: <Link to={conversationId ? `/c/${conversationId}` : '/'}>查数</Link> },
          { key: 'metadata', icon: <DatabaseOutlined />, label: <Link to="/metadata">元数据</Link> },
          ...(canManage ? [{ key: 'models', icon: <SettingOutlined />, label: <Link to="/models">模型配置</Link> }] : []),
          ...(user?.admin ? [{ key: 'users', icon: <TeamOutlined />, label: <Link to="/users">用户与权限</Link> }] : []),
        ]}
      />
      {(!user || user.admin) && <StatusBar />}
      {user && <Dropdown trigger={['click']} menu={{ items: [
        { key: 'domains', label: user.admin ? '全部数据表' : user.domains.map((item) => item.name).join('、') || '未分配业务域', disabled: true },
        { key: 'logout', label: '退出登录', onClick: () => { void logout().catch(() => message.error('退出失败，请重试')) } },
      ] }}>
        <Button className="account-menu" aria-label={`账号菜单 ${user.name}`}>
          <span>{user.name}</span>
        </Button>
      </Dropdown>}
    </header>
  )
}

function Workspace() {
  const { user, canManage } = useAuth()
  return (
    <HealthProvider>
      <ModelProvider>
        <ChatProvider>
          <div className="app">
            <Header />
            <div className="app-body">
              <Suspense fallback={<div className="page-loading"><Spin /></div>}>
                <Routes>
                  <Route path="/" element={<QueryPage />} />
                  <Route path="/c/:conversationId" element={<QueryPage />} />
                  <Route path="/metadata" element={<MetadataPage />} />
                  <Route path="/models" element={canManage ? <ModelSettingsPage /> : <Navigate to="/" replace />} />
                  <Route path="/users" element={user?.admin ? <UserAdministrationPage /> : <Navigate to="/" replace />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
              </Suspense>
            </div>
          </div>
        </ChatProvider>
      </ModelProvider>
    </HealthProvider>
  )
}

export default function App() {
  return <AuthProvider><Workspace /></AuthProvider>
}
