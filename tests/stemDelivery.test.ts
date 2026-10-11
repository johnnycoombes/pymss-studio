import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DJ_NAMING_TEMPLATE,
  DJ_STEMS,
  OGG_MIN_BITRATE_QUALITY,
  OGG_QUALITY_DEFAULT,
  clampOggQuality,
  isStockNamingTemplate,
  modelSupportsDjStems,
  resolveOggQuality,
} from '../src/audio/delivery.ts'

test('ogg quality stays inside q5 to q8 and the preset forces q8', () => {
  assert.equal(clampOggQuality('nope'), OGG_QUALITY_DEFAULT)
  assert.equal(clampOggQuality(4), 5)
  assert.equal(clampOggQuality(6.4), 6)
  assert.equal(clampOggQuality(9), 8)
  assert.equal(resolveOggQuality(6, false), 6)
  assert.equal(resolveOggQuality(5, true), OGG_MIN_BITRATE_QUALITY)
})

test('4-stem DJ output requires vocals, bass, and drums', () => {
  assert.equal(modelSupportsDjStems(['Vocals', 'Other', 'Bass', 'Drums']), true)
  assert.equal(modelSupportsDjStems(['vocals', 'guitar', 'piano', 'bass', 'drums', 'other']), true)
  assert.equal(modelSupportsDjStems(['vocals', 'instrumental']), false)
  assert.equal(modelSupportsDjStems([]), false)
  assert.deepEqual([...DJ_STEMS], ['Vocals', 'Melody', 'Bass', 'Drums'])
})

test('stock naming templates switch to the DJ filename pattern', () => {
  assert.equal(isStockNamingTemplate('%filename%_%stem%_%model%'), true)
  assert.equal(isStockNamingTemplate(''), true)
  assert.equal(isStockNamingTemplate('Live %filename% %stem%'), false)
  assert.equal(DJ_NAMING_TEMPLATE, '%filename% - %stem%')
})
