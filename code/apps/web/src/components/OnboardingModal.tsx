// 首次登录引导（M5 界面引导优化）
// 触发条件：admin（clearance >= 40）+ 所有 KB doc_count = 0 + localStorage 无 kagent_onboarded 标记
// 作用：告诉管理员生产部署后该做哪三件事，并提供快捷入口
import { Button, Modal, Space, Steps, Typography } from 'antd'
import {
  BookOutlined,
  CheckCircleOutlined,
  MessageOutlined,
  UploadOutlined,
} from '@ant-design/icons'

const { Paragraph, Text } = Typography

type Props = {
  open: boolean
  onClose: () => void
  onNavigate: (path: string) => void
}

// 三步引导：每步一个快捷入口按钮，点击后 Modal 关闭并跳转到对应页面
const STEPS = [
  {
    title: '创建业务知识库',
    icon: <BookOutlined />,
    desc: '系统预置了一个空的「公共知识库」，建议先创建一个业务库，比如「员工手册」或「产品文档」',
    target: '/knowledge-bases',
    action: '去新建知识库',
  },
  {
    title: '上传文档',
    icon: <UploadOutlined />,
    desc: '往新建的知识库中上传 PDF / Markdown / Word / Excel 等格式的文件',
    target: '/documents',
    action: '去上传文档',
  },
  {
    title: '开始提问',
    icon: <MessageOutlined />,
    desc: '回到知识问答页，选择知识库即可获得带引用的专业回答',
    target: '/chat',
    action: '去知识问答',
  },
]

export function OnboardingModal({ open, onClose, onNavigate }: Props) {
  return (
    <Modal
      open={open}
      title={null}
      closable={false}
      maskClosable={false}
      keyboard={false}
      footer={null}
      width={580}
    >
      <div style={{ textAlign: 'center', padding: '8px 0 20px' }}>
        <CheckCircleOutlined
          style={{ fontSize: 44, color: '#1677ff', marginBottom: 8 }}
        />
        <Typography.Title level={4} style={{ margin: 0 }}>
          欢迎！完成这三步开启你的知识库
        </Typography.Title>
        <Paragraph type="secondary" style={{ marginTop: 6, marginBottom: 0 }}>
          系统已预置一个空的公共知识库，建议先创建业务知识库开始使用
        </Paragraph>
      </div>

      <Steps
        direction="vertical"
        size="small"
        current={-1}
        items={STEPS.map((s) => ({
          icon: s.icon,
          title: <Text strong>{s.title}</Text>,
          description: (
            <Space direction="vertical" size={6} style={{ width: '100%', marginTop: 4 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {s.desc}
              </Text>
              <Button
                type="primary"
                size="small"
                style={{ width: 140 }}
                onClick={() => {
                  onNavigate(s.target)
                  onClose()
                }}
              >
                {s.action}
              </Button>
            </Space>
          ),
        }))}
      />

      <div style={{ textAlign: 'center', marginTop: 20 }}>
        <Button type="link" onClick={onClose}>
          跳过引导，我自己看看
        </Button>
      </div>
    </Modal>
  )
}
