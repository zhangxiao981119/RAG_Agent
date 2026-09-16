import { create } from 'zustand'

type LayoutState = {
  sidebarOpen: boolean
  setSidebarOpen: (open: boolean) => void
}

export const useLayoutStore = create<LayoutState>((set) => ({
  sidebarOpen: false,
  setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
}))
