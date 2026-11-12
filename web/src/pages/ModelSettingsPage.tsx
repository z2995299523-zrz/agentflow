/** 管理会话可选模型；连接凭据留在服务端。 */
import { PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Form, Input, Modal, Select, Space, Switch, Table, Tag, Tooltip, Typography } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { api, errorText } from '../api/client'
import type { ModelConfig, ModelConfigInput, ModelConfigs, ModelConnectionTest } from '../api/types'
import { useHealth } from '../state/health'
import { useModel } from '../state/model'

const providerNames: Record<string, string> = { custom: '自定义接口', dashscope: '阿里云百炼', deepseek: 'DeepSeek', mimo: '小米 MiMo', zhipu: '智谱', openai: 'OpenAI' }

export default function ModelSettingsPage() {
  const { message, modal } = App.useApp()
  const { refresh } = useModel()
  const { reload: reloadHealth } = useHealth()
  const [form] = Form.useForm<ModelConfigInput>()
  const provider = Form.useWatch('provider', form)
  const [data, setData] = useState<ModelConfigs | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [settingDefault, setSettingDefault] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<ModelConnectionTest | null>(null)
  const [testError, setTestError] = useState('')
  const [editing, setEditing] = useState<ModelConfig | null>(null)
  const [open, setOpen] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try { setData(await api.modelConfigs()); setError('') }
    catch (reason: unknown) { setError(errorText(reason)) }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void load() }, [load])

  const clearTest = () => {
    setTestResult(null)
    setTestError('')
  }

  const edit = (item?: ModelConfig) => {
    clearTest()
    setEditing(item ?? null)
    form.setFieldsValue({ label: item?.label ?? '', provider: item?.provider ?? 'custom',
      model: item?.model ?? '', enabled: item?.enabled ?? true, base_url: item?.base_url ?? '', api_key: '' })
    setOpen(true)
  }

  const readForm = async () => {
    let values: ModelConfigInput
    try { values = await form.validateFields() } catch { return }
    const { base_url, api_key, ...model } = values
    return model.provider === 'custom' ? { ...model, base_url, api_key } : model
  }

  const reloadModels = async () => {
    await Promise.all([load(), refresh()])
    reloadHealth()
  }

  const test = async () => {
    const values = await readForm()
    if (!values) return
    clearTest()
    setTesting(true)
    try { setTestResult(await api.testModelConnection(values, editing?.id)) }
    catch (reason: unknown) { setTestError(errorText(reason)) }
    finally { setTesting(false) }
  }

  const save = async () => {
    const values = await readForm()
    if (!values) return
    setSaving(true)
    try {
      await api.saveModelConfig(values, editing?.id)
      form.setFieldValue('api_key', '')
      setOpen(false)
      await reloadModels()
      void message.success('模型配置已保存')
    } catch (reason: unknown) { void message.error(errorText(reason)) }
    finally { setSaving(false) }
  }

  const makeDefault = async (item: ModelConfig) => {
    setSettingDefault(item.id)
    try {
      await api.setDefaultModel(item.id)
      await reloadModels()
      void message.success(`新会话默认使用「${item.label}」`)
    } catch (reason: unknown) { void message.error(errorText(reason)) }
    finally { setSettingDefault(null) }
  }

  const remove = (item: ModelConfig) => {
    modal.confirm({
      title: `删除「${item.label}」？`,
      content: '已选择此模型的会话需要重新选择模型，历史消息会保留。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await api.deleteModelConfig(item.id)
          await reloadModels()
          void message.success('模型已删除')
        } catch (reason: unknown) { void message.error(errorText(reason)) }
      },
    })
  }

  const configured = (name: string) => data?.providers.find((provider) => provider.name === name)?.configured ?? false
  return (
    <main className="model-settings-page">
      <div className="model-settings-heading">
        <div><Typography.Title level={3}>模型配置</Typography.Title>
          <Typography.Paragraph type="secondary">新会话使用默认模型，已有会话保留各自选用的模型。</Typography.Paragraph></div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => edit()}>新增模型</Button>
      </div>
      <Alert type="info" showIcon title="服务商连接" description="自定义接口使用你填写的 Base URL 和 API Key。预设服务商使用服务器已有的连接。" />
      {error && <Alert type="error" showIcon title="模型配置读取失败" description={error} action={<Button size="small" onClick={() => void load()}>重试</Button>} />}
      <Card className="model-settings-card" title="模型列表">
        <Table<ModelConfig> rowKey="id" loading={loading} dataSource={data?.models ?? []} pagination={false} scroll={{ x: 800 }}
          columns={[
            { title: '模型名称', dataIndex: 'label', render: (label: string, item) => <Space wrap size={[4, 4]}>
              {label}{item.builtin && <Tag>内置</Tag>}{data?.default === item.id && <Tag color="blue">默认模型</Tag>}
            </Space> },
            { title: '服务商', dataIndex: 'provider', render: (name: string) => providerNames[name] ?? name },
            { title: '实际模型代码', dataIndex: 'model', render: (value: string) => <code>{value}</code> },
            { title: '状态', render: (_, item) => !item.enabled ? <Tag>已停用</Tag> : (item.provider === 'custom' ? item.key_configured : configured(item.provider)) ? <Tag color="success">已配置密钥</Tag> : <Tag color="warning">缺少密钥</Tag> },
            { title: '操作', width: 220, render: (_, item) => <Space size={0}>
              {data?.default !== item.id && <Tooltip title={data?.selectable.includes(item.id) ? undefined : '启用且可用于查数的模型才可设为默认'}>
                <span><Button type="link" loading={settingDefault === item.id}
                  disabled={settingDefault !== null || !data?.selectable.includes(item.id)}
                  onClick={() => void makeDefault(item)}>设为默认</Button></span>
              </Tooltip>}
              <Button type="link" onClick={() => edit(item)}>编辑</Button>
              <Button type="link" danger onClick={() => remove(item)}>删除</Button>
            </Space> },
          ]} />
      </Card>
      <Modal title={editing ? '编辑模型' : '新增模型'} open={open}
        onCancel={() => { if (!testing && !saving) { form.setFieldValue('api_key', ''); setOpen(false) } }} onOk={() => void save()}
        okText="保存" cancelText="取消" confirmLoading={saving} destroyOnHidden
        okButtonProps={{ disabled: testing }} cancelButtonProps={{ disabled: testing || saving }}
        closable={!testing && !saving}
        footer={(_, { OkBtn, CancelBtn }) => <>
          <Button onClick={() => void test()} loading={testing} disabled={saving}>测试连接</Button>
          <CancelBtn />
          <OkBtn />
        </>}>
        <Form form={form} layout="vertical" requiredMark="optional" disabled={testing || saving} onValuesChange={clearTest}>
          <Form.Item name="provider" label="连接方式" rules={[{ required: true, message: '请选择连接方式' }]}>
            <Select options={[{ value: 'custom', label: '自定义接口' },
              ...(data?.providers ?? []).map((item) => ({ value: item.name, label: `${providerNames[item.name] ?? item.name}${item.configured ? '' : '（缺少密钥）'}` }))]} />
          </Form.Item>
          {provider === 'custom' && (
            <Form.Item name="base_url" label="Base URL" extra="填写 OpenAI 兼容接口的公开 HTTPS 地址。"
              rules={[{ required: true, whitespace: true, message: '请输入 Base URL' }, { type: 'url', message: '请输入完整的接口地址' }]}>
              <Input placeholder="https://api.example.com/v1" maxLength={2048} autoComplete="off" />
            </Form.Item>
          )}
          <Form.Item name="model" label="模型名称（Model ID）" extra={provider === 'dashscope'
            ? '填写百炼控制台的模型代码，例如 qwen3.8-27b 或 deepseek-v4-pro-0813。修改会影响已选择此入口的会话。'
            : '填写服务商控制台提供的模型代码。'}
            rules={[{ required: true, whitespace: true, message: '请输入模型代码' }, { pattern: /^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,99}$/, message: '请检查模型代码格式' }]}>
            <Input placeholder="例如 qwen3.8-27b" maxLength={100} />
          </Form.Item>
          {provider === 'custom' && (
            <Form.Item name="api_key" label="API Key" extra={editing?.key_configured ? '留空保留现有密钥，更换地址时请重新填写。' : undefined}
              rules={[{ required: !editing?.key_configured, whitespace: true, message: '请输入 API Key' }]}>
              <Input.Password autoComplete="new-password" maxLength={4096} />
            </Form.Item>
          )}
          <Form.Item name="label" label="显示名称" extra="留空时使用模型名称。" rules={[{ max: 60 }]}>
            <Input placeholder="例如：百炼 Kimi" maxLength={60} />
          </Form.Item>
          <Form.Item name="enabled" label="在会话中可选" valuePropName="checked" extra="停用后，已有会话需选择其他模型。">
            <Switch />
          </Form.Item>
        </Form>
        {testError && <Alert type="error" showIcon title="测试连接失败" description={testError} />}
        {testResult && <Alert type={testResult.ok ? 'success' : 'error'} showIcon
          title={testResult.ok ? '测试连接成功' : '测试连接失败'}
          description={`${testResult.message}（${testResult.status_code ? `HTTP ${testResult.status_code} · ` : ''}${testResult.elapsed_ms} 毫秒）`} />}
      </Modal>
    </main>
  )
}
