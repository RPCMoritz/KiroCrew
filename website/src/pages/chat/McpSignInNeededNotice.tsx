import { memo } from 'react'
import { Link } from 'react-router-dom'

import NoticeCard from './NoticeCard'
import { MCP_SERVERS_HREF } from '../connections/connectionsLinks'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'

/** The row a chat shows once when its MCP servers wait for a sign-in
 *  (`meta.kind === 'mcp_sign_in_needed'`, written by
 *  `src/kiro_crew/dashboard/chat_turn/mcp_session.py`). */
export function isMcpSignInNeededNotice(m: ChatMessage): boolean {
  return m.role === 'notice' && m.meta?.kind === 'mcp_sign_in_needed'
}

/**
 * That row, with a link to the MCP Servers page, where each server's row signs
 * in. The row's own text names the servers and the message that signs in from
 * the chat, so a surface without this card still says everything; the link is
 * what this page adds. It starts no sign-in by itself.
 */
export default memo(function McpSignInNeededNotice({ content }: { content: string }) {
  useLanguageGeneration()
  return (
    <NoticeCard
      content={content}
      action={
        <Link
          to={MCP_SERVERS_HREF}
          data-testid="mcp-sign-in-needed-open"
          className="whitespace-nowrap underline underline-offset-2 hover:text-text focus-visible:text-text"
        >
          {i18nT('pages.chatPage.mcp_servers_2')}
        </Link>
      }
    />
  )
})
