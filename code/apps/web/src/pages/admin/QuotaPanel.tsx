// 配额管理面板 —— 单用户/租户双层 token 限额（M6 续篇）
// 上半部：当前用量与上限的对照（进度条直观展示剩余配额）
// 下半部：admin 可编辑租户配额三项数值
import { useCallback, useEffect, useState } from 'react'
import { App, Button, Card, Col, Form, InputNumber, Modal, Row, Skeleton, Statistic, Typography } from 'antd'
import { EditOutlined, ReloadOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'

import {
  fetchQuota,
  fetchQuotaUsage,
  updateQuota,
  type QuotaUsage,
  type TenantQuota,
} from '../../mocks/data'
import { errMsg } from './common'

const { Text } = Typography

// 用量/上限 → 百分比文字（用于进度条着色）
function percentColor(p: number): string {
  if (p >= 90) return '#cf1322' // 红
  if (p >= 70) return '#d48806' // 橙
  return '#3f8600' // 绿
}

export function QuotaPanel() {
  const { message } = App.useApp()
  const [quota, setQuota] = useState<TenantQuota | null>(null)
  const [usage, setUsage] = useState<QuotaUsage | null>(null)
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [q, u] = await Promise.all([fetchQuota(), fetchQuotaUsage()])
      setQuota(q)
      setUsage(u)
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', gap: 12, alignItems: 'center' }}>
        <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
          刷新
        </Button>
        <Button icon={<EditOutlined />} onClick={() => setEditing(true)} disabled={!quota}>
          编辑配额
        </Button>
        {quota && (
          <Text type="secondary">
            最近更新：{dayjs(quota.updated_at).format('YYYY-MM-DD HH:mm:ss')}
          </Text>
        )}
      </div>

      {loading || !quota || !usage ? (
        <Skeleton active />
      ) : (
        <>
          <Card title="今日用量" variant="borderless" style={{ marginBottom: 16 }}>
            <Row gutter={[16, 16]}>
              <Col xs={24} sm={8}>
                <Statistic
                  title="用户今日 token"
                  value={usage.user_tokens_today}
                  suffix={`/ ${usage.user_daily_token_limit}`}
                />
                <Text
                  type="secondary"
                  style={{ color: percentColor((usage.user_tokens_today / usage.user_daily_token_limit) * 100) }}
                >
                  已用 {Math.min(100, Math.round((usage.user_tokens_today / usage.user_daily_token_limit) * 100))}%
                </Text>
              </Col>
              <Col xs={24} sm={8}>
                <Statistic
                  title="用户今日问答次数"
                  value={usage.user_messages_today}
                  suffix={`/ ${usage.user_daily_message_limit}`}
                />
              </Col>
              <Col xs={24} sm={8}>
                <Statistic
                  title="租户今日 token"
                  value={usage.tenant_tokens_today}
                  suffix={`/ ${usage.tenant_daily_token_limit}`}
                />
                <Text
                  type="secondary"
                  style={{
                    color: percentColor((usage.tenant_tokens_today / usage.tenant_daily_token_limit) * 100),
                  }}
                >
                  已用{' '}
                  {Math.min(100, Math.round((usage.tenant_tokens_today / usage.tenant_daily_token_limit) * 100))}%
                </Text>
              </Col>
            </Row>
          </Card>

          <Card title="租户配额上限" variant="borderless">
            <Row gutter={[16, 16]}>
              <Col xs={24} sm={8}>
                <Statistic title="每日 token 上限" value={quota.daily_token_limit} />
              </Col>
              <Col xs={24} sm={8}>
                <Statistic title="月度 token 上限" value={quota.monthly_token_limit} />
              </Col>
              <Col xs={24} sm={8}>
                <Statistic
                  title="月度已用"
                  value={usage.tenant_tokens_this_month}
                  suffix={`/ ${quota.monthly_token_limit}`}
                />
                <Text
                  type="secondary"
                  style={{
                    color: percentColor((usage.tenant_tokens_this_month / quota.monthly_token_limit) * 100),
                  }}
                >
                  已用{' '}
                  {Math.min(
                    100,
                    Math.round((usage.tenant_tokens_this_month / quota.monthly_token_limit) * 100),
                  )}
                  %
                </Text>
              </Col>
            </Row>
            <Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
              用户每日 token 上限 = {usage.user_daily_token_limit}（走全局默认值，不可在租户层覆盖）
            </Text>
          </Card>
        </>
      )}

      {editing && quota && (
        <QuotaEditModal
          quota={quota}
          onClose={() => setEditing(false)}
          onSaved={async () => {
            setEditing(false)
            await load()
          }}
        />
      )}
    </div>
  )
}

// ── 编辑配额弹窗 ──────────────────────────────────────────
function QuotaEditModal({
  quota,
  onClose,
  onSaved,
}: {
  quota: TenantQuota
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm<{
    daily_token_limit: number
    daily_message_limit: number
    monthly_token_limit: number
  }>()
  const [saving, setSaving] = useState(false)

  async function handleSubmit(values: {
    daily_token_limit: number
    daily_message_limit: number
    monthly_token_limit: number
  }) {
    setSaving(true)
    try {
      await updateQuota({
        daily_token_limit: values.daily_token_limit,
        daily_message_limit: values.daily_message_limit,
        monthly_token_limit: values.monthly_token_limit,
      })
      message.success('配额已更新，立即生效')
      await onSaved()
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title="编辑租户配额"
      open
      onCancel={onClose}
      confirmLoading={saving}
      okText="保存"
      cancelText="取消"
      onOk={() => void form.submit()}
      destroyOnClose
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          daily_token_limit: quota.daily_token_limit,
          daily_message_limit: quota.daily_message_limit,
          monthly_token_limit: quota.monthly_token_limit,
        }}
        onFinish={handleSubmit}
        style={{ marginTop: 16 }}
      >
        <Form.Item
          label="每日 token 上限（输入+输出合计）"
          name="daily_token_limit"
          rules={[{ required: true, message: '请输入' }, { type: 'number', min: 1, message: '必须大于 0' }]}
        >
          <InputNumber style={{ width: '100%' }} min={1} step={10000} />
        </Form.Item>
        <Form.Item
          label="每日问答次数上限"
          name="daily_message_limit"
          rules={[{ required: true, message: '请输入' }, { type: 'number', min: 1, message: '必须大于 0' }]}
        >
          <InputNumber style={{ width: '100%' }} min={1} step={10} />
        </Form.Item>
        <Form.Item
          label="月度 token 上限（防单租户刷爆整月预算）"
          name="monthly_token_limit"
          rules={[{ required: true, message: '请输入' }, { type: 'number', min: 1, message: '必须大于 0' }]}
        >
          <InputNumber style={{ width: '100%' }} min={1} step={100000} />
        </Form.Item>
      </Form>
    </Modal>
  )
}
