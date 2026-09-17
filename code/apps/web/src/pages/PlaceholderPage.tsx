import { Card, Empty, Typography } from 'antd'

const { Title, Text } = Typography

type PlaceholderPageProps = {
  title: string
  description: string
}

export function PlaceholderPage({ title, description }: PlaceholderPageProps) {
  return (
    <Card variant="borderless">
      <Text style={{ color: '#1677ff', fontSize: 13 }}>M0 基础框架</Text>
      <Title level={3} style={{ marginTop: 8 }}>
        {title}
      </Title>
      <Text type="secondary">{description}</Text>
      <div style={{ marginTop: 32 }}>
        <Empty description="功能将在后续里程碑实现" />
      </div>
    </Card>
  )
}
