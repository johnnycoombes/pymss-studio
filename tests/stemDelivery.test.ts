import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DJ_NAMING_TEMPLATE,
  DJ_OGG_FILE_STEMS,
  DJ_STEMS,
  OGG_MIN_BITRATE_QUALITY,
  OGG_QUALITY_DEFAULT,
  clampOggQuality,
  isStockNamingTemplate,
  modelSupportsDjStems,
  resolveOggQuality,
  xdjTrackFolderName,
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

test('ogg dj files use the xdj-az names and melody is other', () => {
  assert.deepEqual(
    DJ_STEMS.map(name => DJ_OGG_FILE_STEMS[name]),
    ['vocals', 'other', 'bass', 'drums'],
  )
  for (const fileStem of Object.values(DJ_OGG_FILE_STEMS)) {
    assert.equal(fileStem.includes('inst'), false)
    if (fileStem !== 'other') assert.equal(fileStem.includes('other'), false)
    if (fileStem !== 'vocals') assert.equal(fileStem.includes('vocal'), false)
  }
})

test('xdj folder names keep a trailing space or dot and escape device names', () => {
  assert.equal(xdjTrackFolderName('song.wav'), 'song')
  assert.equal(xdjTrackFolderName('My Track.flac'), 'My Track')
  assert.equal(xdjTrackFolderName('song.'), 'song._')
  assert.equal(xdjTrackFolderName('song '), 'song _')
  assert.equal(xdjTrackFolderName('CON'), 'CON_')
  assert.equal(xdjTrackFolderName('nul.wav'), 'nul_')
  assert.equal(xdjTrackFolderName('COM1.'), 'COM1._')
  assert.equal(xdjTrackFolderName('folder/COM1.live.wav'), 'COM1.live_')
  assert.equal(xdjTrackFolderName('AUX'), 'AUX_')
})
