import { describe, expect, it } from 'vitest'
import { latestMutationError } from './latestMutationError'

describe('latestMutationError', () => {
  const failed = new Error('refused')

  it('is null once a later action succeeded', () => {
    expect(latestMutationError({ submittedAt: 1, error: failed }, { submittedAt: 2, error: null })).toBeNull()
  })

  it('reports a newer failure over an older one', () => {
    const newer = new Error('newer')
    expect(latestMutationError({ submittedAt: 1, error: failed }, { submittedAt: 2, error: newer })).toBe(newer)
  })

  it('lets the later-listed mutation win a same-millisecond tie', () => {
    const later = new Error('later')
    expect(latestMutationError({ submittedAt: 5, error: failed }, { submittedAt: 5, error: later })).toBe(later)
  })

  it('ignores mutations that never ran', () => {
    expect(latestMutationError({ submittedAt: 0, error: null }, { submittedAt: 3, error: failed })).toBe(failed)
    expect(latestMutationError({ submittedAt: 0, error: null }, { submittedAt: 0, error: null })).toBeNull()
  })
})
