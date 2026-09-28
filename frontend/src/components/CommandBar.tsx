import { useState } from "react"

function CommandBar() {
  const [command, setCommand] = useState("")

  return (
    <footer className="command-dock">
      <form className="command-bar" onSubmit={(event) => event.preventDefault()}>
        <span className="command-symbol" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M8 9l3 3-3 3M13 15h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
        <label htmlFor="desk-command" className="sr-only">Ask your AI desk</label>
        <input
          id="desk-command"
          value={command}
          onChange={(event) => setCommand(event.target.value)}
          placeholder="Ask your AI desk..."
          autoComplete="off"
        />
        <span className="command-hint"><kbd>⌘</kbd><kbd>K</kbd></span>
        <button type="submit" className="send-command" aria-label="Send command">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M5 12h13M13 7l5 5-5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </form>
      <div className="dock-meta">
        <span><i /> SECURE LOCAL SESSION</span>
        <span>AI CAN MAKE MISTAKES. VERIFY CRITICAL DATA.</span>
        <span>FRIDAY / 01</span>
      </div>
    </footer>
  )
}

export default CommandBar
