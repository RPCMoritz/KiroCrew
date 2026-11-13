/**
 * The App SDK's `ProjectPicker` mounts only for an app that declares what it calls.
 *
 * An external app gets the dashboard's picker once its `permissions.api` covers
 * every endpoint family the picker calls (`PROJECT_PICKER_PATHS`); with one
 * missing it renders a refusal naming the path and mounts nothing. A builtin page
 * gets the picker as before, and a surface with no app identity fails closed.
 *
 * The host picker is stubbed here: what is under test is which surfaces mount it
 * and with what props. The last block pins the other half of the claim against
 * the real picker's source and the real dashboard client: every endpoint the
 * picker can call is inside the declared families, so the check at mount covers
 * every request it can send.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

const hostPicker = vi.hoisted(() => ({ props: [] as Record<string, unknown>[] }))
vi.mock('../components/ProjectPicker', () => ({
  default: (props: Record<string, unknown>) => {
    hostPicker.props.push(props)
    return <div data-testid="host-project-picker" />
  },
}))

import { AppApiProvider, ProjectPicker } from '../app-sdk/index'
import { AppIdentityProvider } from '../app-sdk/identity'
import { apiPathAllowed } from '../app-sdk/scopedApi'
import { PROJECT_PICKER_PATHS, undeclaredProjectPickerPath } from '../app-sdk/ProjectPicker'
import pickerSource from '../components/ProjectPicker.tsx?raw'
import newFolderSource from '../components/ProjectPickerNewFolder.tsx?raw'

let fetchMock: ReturnType<typeof vi.fn>
let appSeq = 0

beforeEach(() => {
  hostPicker.props.length = 0
  fetchMock = vi.fn(async () => new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function externalApp(allowedApiPaths: string[], children: ReactNode) {
  appSeq += 1
  return (
    <AppApiProvider appName={`picker-app-${appSeq}`} allowedApiPaths={allowedApiPaths} navigateFn={() => {}}>
      {children}
    </AppApiProvider>
  )
}

const onSelect = vi.fn()
const onOpenChange = vi.fn()
const picker = (open = true) => (
  <ProjectPicker open={open} onOpenChange={onOpenChange} onSelect={onSelect} startPath="/work/repo" />
)

describe('undeclaredProjectPickerPath', () => {
  it('names the first family a declaration does not cover', () => {
    expect(undeclaredProjectPickerPath([])).toBe('/api/browse-dirs')
    expect(undeclaredProjectPickerPath(['/api/browse-dirs'])).toBe('/api/recent-projects')
    expect(undeclaredProjectPickerPath(['/api/browse-dirs', '/api/recent-projects'])).toBe('/api/favorite-projects')
    // A child of a family is not the family: it would leave the listing undeclared.
    expect(undeclaredProjectPickerPath(['/api/browse-dirs/create', '/api/recent-projects', '/api/favorite-projects'])).toBe('/api/browse-dirs')
  })

  it('is null once every family is covered, by bare paths or a wildcard', () => {
    expect(undeclaredProjectPickerPath([...PROJECT_PICKER_PATHS])).toBeNull()
    expect(undeclaredProjectPickerPath(['/api/*'])).toBeNull()
  })
})

describe('the App SDK ProjectPicker', () => {
  it('refuses an external app that declares none of the families, and mounts nothing', () => {
    render(externalApp(['/api/project-scaffold'], picker()))
    expect(screen.getByTestId('app-project-picker-not-declared')).toHaveTextContent('/api/browse-dirs')
    expect(screen.queryByTestId('host-project-picker')).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('refuses an external app that declares only some of them, naming the missing one', () => {
    render(externalApp(['/api/browse-dirs', '/api/favorite-projects'], picker()))
    expect(screen.getByTestId('app-project-picker-not-declared')).toHaveTextContent('/api/recent-projects')
    expect(screen.queryByTestId('host-project-picker')).toBeNull()
  })

  it('fails closed with no app identity and no SDK provider', () => {
    render(picker())
    expect(screen.getByTestId('app-project-picker-not-declared')).toBeInTheDocument()
    expect(screen.queryByTestId('host-project-picker')).toBeNull()
  })

  it('mounts the dashboard picker for an external app that declares every family', () => {
    render(externalApp([...PROJECT_PICKER_PATHS], picker()))
    expect(screen.getByTestId('host-project-picker')).toBeInTheDocument()
    expect(hostPicker.props).toHaveLength(1)
    const props = hostPicker.props[0]
    expect(props.onSelect).toBe(onSelect)
    expect(props.onOpenChange).toBe(onOpenChange)
    expect(props.startPath).toBe('/work/repo')
    expect(props.open).toBe(true)
    // The chat hand-off would unmount the app page under the user.
    expect(props.errorHandoff).toBeUndefined()
    expect(props.footer).toBeUndefined()
  })

  it('mounts the dashboard picker for a builtin page whatever it declares', () => {
    render(<AppIdentityProvider appId="project-scaffolder" origin="builtin">{picker()}</AppIdentityProvider>)
    expect(screen.getByTestId('host-project-picker')).toBeInTheDocument()
  })

  it('renders nothing while closed, refusal included', () => {
    render(externalApp([], picker(false)))
    expect(screen.queryByTestId('app-project-picker-not-declared')).toBeNull()
    expect(screen.queryByTestId('host-project-picker')).toBeNull()
  })
})

describe('the declared families cover every request the dashboard picker can send', () => {
  // The dashboard client members the picker and its New folder form call.
  const PICKER_MEMBERS = [
    'addFavoriteProject',
    'browseDirs',
    'browseDrives',
    'createBrowseDir',
    'favoriteProjects',
    'recentProjects',
    'removeFavoriteProject',
  ]

  it('names exactly the members the picker source calls', () => {
    const called = new Set<string>()
    for (const source of [pickerSource, newFolderSource]) {
      for (const match of source.matchAll(/\bapi\.(\w+)\(/g)) called.add(match[1])
    }
    expect([...called].sort()).toEqual(PICKER_MEMBERS)
  })

  it('sends each member to a path inside the declared families', async () => {
    const { api } = (await vi.importActual('../api/client')) as { api: Record<string, (...args: string[]) => Promise<unknown>> }
    for (const member of PICKER_MEMBERS) {
      fetchMock.mockClear()
      await api[member]('/work/repo', 'child').catch(() => undefined)
      expect(fetchMock, member).toHaveBeenCalled()
      for (const call of fetchMock.mock.calls) {
        const path = new URL(String(call[0]), 'http://localhost').pathname
        expect(apiPathAllowed(PROJECT_PICKER_PATHS, path), `${member} -> ${path}`).toBe(true)
      }
    }
  })
})
