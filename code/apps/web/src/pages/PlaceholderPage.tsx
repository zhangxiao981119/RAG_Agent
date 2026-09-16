type PlaceholderPageProps = {
  title: string
  description: string
}

export function PlaceholderPage({ title, description }: PlaceholderPageProps) {
  return (
    <section>
      <p className="mb-2 text-sm font-medium text-blue-600">M0 基础框架</p>
      <h1 className="text-3xl font-bold tracking-tight">{title}</h1>
      <p className="mt-3 max-w-2xl text-slate-600">{description}</p>
      <div className="mt-8 rounded-2xl border border-dashed border-slate-300 bg-white p-10 text-center text-slate-500">
        功能将在后续里程碑实现
      </div>
    </section>
  )
}
