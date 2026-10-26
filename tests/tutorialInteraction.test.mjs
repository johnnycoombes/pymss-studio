import assert from 'node:assert/strict'
import test from 'node:test'
import { Window } from 'happy-dom'
import { lockTutorialInteraction } from '../src/features/onboarding/demoInteraction.ts'

test('tutorial locking permits drawer dismissal and the guide while keeping other controls locked', () => {
  const browser = new Window()
  const names = ['window', 'document', 'Element', 'KeyboardEvent']
  const previous = names.map(name => Object.getOwnPropertyDescriptor(globalThis, name))
  for (const name of names) Object.defineProperty(globalThis, name, { configurable: true, value: name === 'window' ? browser : browser[name] })
  browser.document.body.innerHTML = '<button id="active"></button><button id="other"></button>'
    + '<section class="beginner-guide-coach"><button id="next"></button></section>'
    + '<div class="n-drawer-mask"></div><section class="n-drawer"><button class="n-drawer-header__close"></button></section>'
  const active = document.querySelector('#active'), other = document.querySelector('#other')
  let parameters = false
  const unlock = lockTutorialInteraction(() => active, () => parameters)
  try {
    let otherClicks = 0, nextClicks = 0, closes = 0, masks = 0
    other.addEventListener('click', () => otherClicks++)
    document.querySelector('#next').addEventListener('click', () => nextClicks++)
    document.querySelector('.n-drawer-header__close').addEventListener('click', () => closes++)
    document.querySelector('.n-drawer-mask').addEventListener('click', () => masks++)
    other.click()
    document.querySelector('#next').click()
    document.querySelector('.n-drawer-header__close').click()
    document.querySelector('.n-drawer-mask').click()
    document.body.dispatchEvent(new browser.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    assert.equal(otherClicks, 0)
    assert.equal(nextClicks, 1)
    assert.equal(closes, 2)
    assert.equal(masks, 1)
    const drop = new browser.Event('drop', { bubbles: true, cancelable: true })
    active.dispatchEvent(drop)
    assert.equal(drop.defaultPrevented, true)
    parameters = true
    document.querySelector('.n-drawer-mask').click()
    document.body.dispatchEvent(new browser.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    assert.equal(masks, 1)
    assert.equal(closes, 2)
    unlock()
    other.click()
    assert.equal(otherClicks, 1)
  } finally {
    unlock()
    browser.happyDOM.abort()
    names.forEach((name, index) => previous[index] ? Object.defineProperty(globalThis, name, previous[index]) : Reflect.deleteProperty(globalThis, name))
  }
})
