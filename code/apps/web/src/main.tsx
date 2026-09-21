import React from 'react'
import { Component, ErrorInfo, ReactNode } from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import { App as AntdApp, ConfigProvider, Result, Button } from 'antd'
import zhCN from 'antd/locale/zh_CN'

import App from './App'
import './index.css'

const queryClient = new QueryClient()

// 全局错误边界：捕获渲染期未处理异常，避免整树卸载白屏
class GlobalErrorBoundary extends Component<
  { children: ReactNode },
  { hasError: boolean; error?: Error }
> {
  state: { hasError: boolean; error?: Error } = { hasError: false }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 上报错误（这里只 console.error，生产可接 Sentry/自建上报）
    console.error('GlobalErrorBoundary caught:', error, info)
  }

  handleReload = () => {
    this.setState({ hasError: false, error: undefined })
    window.location.reload()
  }

  render() {
    if (this.state.hasError) {
      return (
        <ConfigProvider locale={zhCN}>
          <Result
            status="500"
            title="页面出错了"
            subTitle="抱歉，应用遇到未知错误。请尝试刷新页面。"
            extra={<Button type="primary" onClick={this.handleReload}>刷新页面</Button>}
          />
        </ConfigProvider>
      )
    }
    return this.props.children
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <GlobalErrorBoundary>
      <ConfigProvider
        locale={zhCN}
        theme={{
          token: {
            colorPrimary: '#1677ff',
            borderRadius: 8,
          },
        }}
      >
        <AntdApp>
          <QueryClientProvider client={queryClient}>
            <BrowserRouter>
              <App />
            </BrowserRouter>
          </QueryClientProvider>
        </AntdApp>
      </ConfigProvider>
    </GlobalErrorBoundary>
  </React.StrictMode>,
)
