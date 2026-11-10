import { NAVIGATION } from "../services/navigation"
import type { Screen } from "../services/navigation"

export default function Sidebar({ screen }: { screen: Screen }) {
  return (
    <aside className="sidebar">
      <a className="sidebar-brand" href="#home" aria-label="FRIDAY Home">F<span>FRIDAY / DESK</span></a>
      <nav aria-label="Main navigation">
        {NAVIGATION.map((item, index) => (
          <a key={item.id} href={`#${item.id}`} aria-current={screen === item.id ? "page" : undefined}>
            <span aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
            {item.label}
          </a>
        ))}
      </nav>
      <p>RESEARCH ONLY<br />EXECUTION DISABLED</p>
    </aside>
  )
}