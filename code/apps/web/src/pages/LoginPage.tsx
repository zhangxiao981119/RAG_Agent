import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Alert, Button, Card, Form, Input } from 'antd'
import { LockOutlined, RobotOutlined, UserOutlined } from '@ant-design/icons'

import { login, User } from '../mocks/data'

type Props = {
  onLogin: (user: User) => void
}

type LoginFormValues = {
  username: string
  password: string
}

export function LoginPage({ onLogin }: Props) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()

  async function handleSubmit(values: LoginFormValues) {
    setLoading(true)
    setError(null)
    try {
      const user = await login(values.username.trim(), values.password)
      onLogin(user)
      navigate('/chat')
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }

  return (
    // 深蓝渐变背景铺满一屏；overflow hidden 防止装饰光斑把页面撑出滚动条
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
        position: 'relative',
        overflow: 'hidden',
        background: 'linear-gradient(135deg, #0a1f44 0%, #103a7d 45%, #1677ff 100%)',
      }}
    >
      {/* 装饰光斑：纯 CSS 模糊圆，增强背景层次 */}
      <div
        style={{
          position: 'absolute',
          width: 420,
          height: 420,
          borderRadius: '50%',
          background: 'rgba(64, 150, 255, 0.35)',
          filter: 'blur(90px)',
          top: -120,
          left: -100,
          pointerEvents: 'none',
        }}
      />
      <div
        style={{
          position: 'absolute',
          width: 360,
          height: 360,
          borderRadius: '50%',
          background: 'rgba(24, 144, 255, 0.28)',
          filter: 'blur(80px)',
          bottom: -100,
          right: -80,
          pointerEvents: 'none',
        }}
      />
      <div
        style={{
          position: 'absolute',
          width: 200,
          height: 200,
          borderRadius: '50%',
          background: 'rgba(255, 255, 255, 0.12)',
          filter: 'blur(60px)',
          top: '40%',
          left: '62%',
          pointerEvents: 'none',
        }}
      />

      <Card
        variant="borderless"
        style={{
          width: 420,
          borderRadius: 16,
          boxShadow: '0 12px 40px rgba(6, 30, 74, 0.45)',
          position: 'relative',
        }}
      >
        {/* 头部：图标 + 标题居中 */}
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <div
            style={{
              width: 56,
              height: 56,
              borderRadius: 14,
              background: 'linear-gradient(135deg, #1677ff, #4096ff)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              margin: '0 auto 16px',
              boxShadow: '0 6px 16px rgba(22, 119, 255, 0.35)',
            }}
          >
            <RobotOutlined style={{ fontSize: 28, color: '#fff' }} />
          </div>
          <div style={{ color: '#1677ff', fontSize: 13, fontWeight: 500 }}>
            知识库问答 Agent
          </div>
          <h1 style={{ fontSize: 24, fontWeight: 700, margin: '8px 0 4px' }}>登录</h1>
          <div style={{ color: 'rgba(0,0,0,0.45)', fontSize: 13 }}>
            使用账号密码登录系统
          </div>
        </div>

        {error && (
          <Alert
            type="error"
            showIcon
            message={error}
            style={{ marginBottom: 16 }}
          />
        )}

        <Form<LoginFormValues>
          layout="vertical"
          initialValues={{ username: '', password: '' }}
          onFinish={handleSubmit}
          requiredMark={false}
        >
          <Form.Item
            label="用户名"
            name="username"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input prefix={<UserOutlined />} placeholder="用户名" autoComplete="username" />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="密码"
              autoComplete="current-password"
            />
          </Form.Item>
          <Form.Item style={{ marginBottom: 12, marginTop: 24 }}>
            <Button type="primary" htmlType="submit" block loading={loading} size="large">
              登录
            </Button>
          </Form.Item>
        </Form>

        <Alert
          type="info"
          showIcon
          message="试试问「报销流程」「员工手册」「考勤制度」会得到带引用的答案；问「股票」「天气」会触发拒答。"
          style={{ fontSize: 12 }}
        />
      </Card>
    </div>
  )
}
