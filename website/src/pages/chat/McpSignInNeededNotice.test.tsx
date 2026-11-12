import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import McpSignInNeededNotice, { isMcpSignInNeededNotice } from './McpSignInNeededNotice'
import { createTranscriptRenderers } from './transcriptRenderers'
import { MCP_SERVERS_HREF } from '../connections/connectionsLinks'
import type { ChatMessage } from '../../types'

const ROW: ChatMessage = {
  role: 'notice',
  content: '⚠ notes needs you to sign in before its tools work in this chat. Send "Sign in to notes" here, or open MCP Servers and sign in there.',
  meta: { kind: 'mcp_sign_in_needed', servers: ['notes'] },
}

describe('McpSignInNeededNotice', () => {
  it('shows the row text and links to the MCP Servers page', () => {
    render(
      <MemoryRouter>
        <McpSignInNeededNotice content={ROW.content} />
      </MemoryRouter>,
    )
    expect(screen.getByText(/notes needs you to sign in/)).toBeInTheDocument()
    const link = screen.getByTestId('mcp-sign-in-needed-open')
    expect(link).toHaveAttribute('href', MCP_SERVERS_HREF)
    // A label from the catalog, never the key.
    expect(link.textContent).not.toMatch(/mcp_servers_2/)
    expect(link.textContent?.trim()).not.toBe('')
  })

  it('claims only a notice row of its kind', () => {
    expect(isMcpSignInNeededNotice(ROW)).toBe(true)
    expect(isMcpSignInNeededNotice({ ...ROW, meta: { kind: 'crew_paused_notice' } })).toBe(false)
    expect(isMcpSignInNeededNotice({ ...ROW, meta: undefined })).toBe(false)
    expect(isMcpSignInNeededNotice({ ...ROW, role: 'assistant' })).toBe(false)
  })

  it('is the row the chat page draws for that kind, ahead of the plain notice', () => {
    const renderers = createTranscriptRenderers({})
    const claimed = renderers.find(r => r.roles.includes('notice') && (r.match ? r.match(ROW) : true))
    expect(claimed?.id).toBe('mcp_sign_in_needed')
  })
})
