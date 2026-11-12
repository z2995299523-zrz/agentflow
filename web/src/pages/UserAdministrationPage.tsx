/** 维护应用账号、业务域和有效期。 */
import { PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Form, Input, Modal, Select, Space, Switch, Table, Tag, Typography } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { api, errorText } from '../api/client'
import type { ManagedUser, UserAdministration } from '../api/types'

type GrantForm = { code: string; starts?: string; expires?: string }
type UserForm = Omit<ManagedUser, 'id' | 'grants'> & { password?: string; grants: GrantForm[] }
type DomainForm = { code: string; name: string }

const auditNames: Record<string, string> = { 'user.save': '保存账号和授权', 'domain.save': '保存业务域', 'access.denied': '拒绝访问' }

function localDate(timestamp: number | null) {
  if (!timestamp) return undefined
  const date = new Date(timestamp * 1000)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}

export default function UserAdministrationPage() {
  const { message } = App.useApp()
  const [form] = Form.useForm<UserForm>()
  const [domainForm] = Form.useForm<DomainForm>()
  const [data, setData] = useState<UserAdministration>()
  const [filter, setFilter] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [editor, setEditor] = useState<ManagedUser | 'new' | null>(null)
  const [domain, setDomain] = useState<DomainForm | 'new' | null>(null)
  const [saveError, setSaveError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try { setData(await api.administration()); setError('') }
    catch (reason: unknown) { setError(errorText(reason)) }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void load() }, [load])

  const edit = (user?: ManagedUser) => {
    setSaveError('')
    setEditor(user ?? 'new')
    form.setFieldsValue({
      username: user?.username ?? '', name: user?.name ?? '', active: user?.active ?? true,
      admin: user?.admin ?? false, password: '',
      grants: user?.grants.map((grant) => ({
        code: grant.code, starts: localDate(grant.starts_at), expires: localDate(grant.expires_at),
      })) ?? [],
    })
  }

  const editDomain = (item?: DomainForm) => {
    setSaveError('')
    setDomain(item ?? 'new')
    domainForm.setFieldsValue({ code: item?.code ?? '', name: item?.name ?? '' })
  }

  const save = async (values: UserForm) => {
    setSaving(true)
    setSaveError('')
    try {
      await api.saveUser({
        ...values,
        password: values.password ?? '',
        grants: values.grants.map((grant) => ({
          code: grant.code, starts_at: grant.starts ? Math.floor(new Date(grant.starts).getTime() / 1000) : 0,
          expires_at: grant.expires ? Math.floor(new Date(grant.expires).getTime() / 1000) : null,
        })),
      }, editor && editor !== 'new' ? editor.id : undefined)
      form.setFieldValue('password', '')
      setEditor(null)
      await load()
      void message.success('账号和授权已保存')
    } catch (reason: unknown) { setSaveError(errorText(reason)) }
    finally { setSaving(false) }
  }

  const saveDomain = async ({ code, name }: DomainForm) => {
    setSaving(true)
    setSaveError('')
    try { await api.saveDomain(code, name); setDomain(null); await load(); void message.success('业务域已保存') }
    catch (reason: unknown) { setSaveError(errorText(reason)) }
    finally { setSaving(false) }
  }

  const names = new Map(data?.domains.map((item) => [item.code, item.name]))
  const users = data?.users.filter((user) => `${user.username} ${user.name}`.toLowerCase().includes(filter.trim().toLowerCase())) ?? []
  const now = Date.now() / 1000

  return (
    <main className="model-settings-page">
      <div className="model-settings-heading">
        <Typography.Title level={3}>用户与权限</Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => edit()}>新增账号</Button>
      </div>
      {error && <Alert type="error" showIcon title={error} action={<Button onClick={() => void load()}>重试</Button>} />}
      <Card title="账号与授权" className="model-settings-card" extra={
        <Input aria-label="搜索账号" placeholder="搜索姓名或账号" value={filter} onChange={(event) => setFilter(event.target.value)} />
      }>
        <Table rowKey="id" loading={loading} dataSource={users} scroll={{ x: 650 }} columns={[
          { title: '姓名', dataIndex: 'name' },
          { title: '账号', dataIndex: 'username' },
          { title: '角色', render: (_, user) => user.admin ? '管理员' : '普通用户' },
          { title: '业务域', render: (_, user) => user.grants.length ? user.grants.map((grant) => (
            <Tag key={grant.code} color={user.active && grant.starts_at <= now && (!grant.expires_at || grant.expires_at > now) ? 'blue' : 'default'}>
              {names.get(grant.code) ?? grant.code}
              {grant.expires_at && grant.expires_at <= now ? '（已到期）' : grant.starts_at > now ? '（待生效）' : ''}
            </Tag>
          )) : '未授权' },
          { title: '状态', render: (_, user) => <Tag color={user.active ? 'success' : 'default'}>{user.active ? '启用' : '停用'}</Tag> },
          { title: '操作', render: (_, user) => <Button type="link" onClick={() => edit(user)}>编辑授权</Button> },
        ]} />
      </Card>
      <Card title="业务域" className="model-settings-card" extra={<Button onClick={() => editDomain()}>登记业务域</Button>}>
        {Boolean(data?.unclassified) && <Alert type="warning" showIcon className="service-alert"
          title={`${data?.unclassified} 张表待归类，暂未开放查询`} />}
        <Table rowKey="code" loading={loading} dataSource={data?.domains ?? []} pagination={false} scroll={{ x: 510 }} columns={[
          { title: '业务域', dataIndex: 'name' }, { title: '域代码', dataIndex: 'code' },
          { title: '表数', dataIndex: 'table_count' }, { title: '授权人数', dataIndex: 'user_count' },
          { title: '操作', render: (_, item) => <Button type="link" onClick={() => editDomain(item)}>编辑名称</Button> },
        ]} />
      </Card>
      <Card title="最近权限记录" className="model-settings-card">
        <Table rowKey="id" dataSource={data?.audit ?? []} pagination={{ pageSize: 8 }} scroll={{ x: 540 }} columns={[
          { title: '时间', render: (_, item) => new Date(item.occurred_at * 1000).toLocaleString() },
          { title: '操作者', render: (_, item) => data?.users.find((user) => user.id === item.actor)?.name ?? '初始化' },
          { title: '动作', render: (_, item) => auditNames[item.action] ?? item.action },
          { title: '对象', render: (_, item) => data?.users.find((user) => user.id === item.target)?.name ?? names.get(item.target) ?? item.target },
        ]} />
      </Card>
      <Modal open={editor !== null} title={editor === 'new' ? '新增账号' : '编辑账号和授权'} okText="保存" cancelText="取消"
        onOk={() => form.submit()} confirmLoading={saving} onCancel={() => { if (!saving) { form.setFieldValue('password', ''); setEditor(null) } }} destroyOnHidden>
        <Form form={form} layout="vertical" onFinish={(values) => void save(values)} disabled={saving}>
          <Form.Item name="username" label="登录账号" rules={[{ required: true, pattern: /^[a-zA-Z0-9][a-zA-Z0-9_.-]{2,39}$/, message: '请输入 3 到 40 位字母、数字或常用连接符' }]}>
            <Input autoComplete="off" maxLength={40} />
          </Form.Item>
          <Form.Item name="name" label="姓名" rules={[{ required: true, whitespace: true }]}><Input maxLength={60} /></Form.Item>
          <Form.Item name="password" label={editor === 'new' ? '初始密码' : '重设密码'}
            extra={editor === 'new' ? '至少 12 个字符。' : '留空保留原密码。重设密码后，该账号需重新登录。'}
            rules={[{ required: editor === 'new', min: 12, message: '密码至少 12 个字符' }]}>
            <Input.Password maxLength={128} autoComplete="new-password" />
          </Form.Item>
          <Space size="large">
            <Form.Item name="active" label="启用账号" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item name="admin" label="管理员" valuePropName="checked"><Switch /></Form.Item>
          </Space>
          <Form.List name="grants">
            {(fields, { add, remove }) => (
              <div className="domain-grants">
                {fields.map((field) => <Card key={field.key} size="small" title="业务域授权"
                  extra={<Button type="link" danger onClick={() => remove(field.name)}>移除</Button>}>
                  <Form.Item label="业务域" name={[field.name, 'code']} rules={[{ required: true }]}>
                    <Select options={data?.domains.map((item) => ({ value: item.code, label: item.name }))} />
                  </Form.Item>
                  <div className="grant-dates">
                    <Form.Item label="生效时间" name={[field.name, 'starts']} extra="留空立即生效"><Input type="datetime-local" /></Form.Item>
                    <Form.Item label="到期时间" name={[field.name, 'expires']} extra="留空长期有效"><Input type="datetime-local" /></Form.Item>
                  </div>
                </Card>)}
                <Button onClick={() => add({})} icon={<PlusOutlined />} disabled={!data?.domains.length}>添加业务域授权</Button>
              </div>
            )}
          </Form.List>
        </Form>
        {saveError && <Alert type="error" showIcon title={saveError} className="grant-error" />}
      </Modal>
      <Modal open={domain !== null} title={domain === 'new' ? '登记业务域' : '编辑业务域名称'} okText="保存" cancelText="取消"
        onOk={() => domainForm.submit()} confirmLoading={saving} onCancel={() => { if (!saving) setDomain(null) }} destroyOnHidden>
        <Form form={domainForm} layout="vertical" onFinish={(values) => void saveDomain(values)} disabled={saving}>
          <Form.Item label="域代码" name="code" rules={[{ required: true, pattern: /^[a-z][a-z0-9_-]{0,31}$/, message: '使用小写字母开头，可含数字、下划线和连字符' }]}>
            <Input disabled={domain !== 'new'} maxLength={32} />
          </Form.Item>
          <Form.Item label="业务域名称" name="name" rules={[{ required: true, whitespace: true }]}><Input maxLength={60} /></Form.Item>
        </Form>
        {saveError && <Alert type="error" showIcon title={saveError} />}
      </Modal>
    </main>
  )
}
