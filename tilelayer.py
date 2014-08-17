# -*- coding: utf-8 -*-
"""
/***************************************************************************
 TileLayer Plugin
                                 A QGIS plugin
 Plugin layer for Tile Maps
                              -------------------
        begin                : 2012-12-16
        copyright            : (C) 2013 by Minoru Akagi
        email                : akaginch@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
# Import the PyQt and QGIS libraries
from PyQt4.QtCore import *
from PyQt4.QtGui import *
from qgis.core import *
from qgis.gui import QgsMessageBar
import os
import math
from tiles import *
from downloader import Downloader
import threading

debug_mode = 1

class LayerDefaultSettings:

  TRANSPARENCY = 0
  BLEND_MODE = "SourceOver"
  SMOOTH_RENDER = True

class TileLayer(QgsPluginLayer):

  LAYER_TYPE = "TileLayer"
  MAX_TILE_COUNT = 256

  def __init__(self, plugin, layerDef, creditVisibility=1, pseudo_mercator=None):
    QgsPluginLayer.__init__(self, TileLayer.LAYER_TYPE, layerDef.title)
    self.plugin = plugin
    self.iface = plugin.iface
    self.layerDef = layerDef
    self.creditVisibility = 1 if creditVisibility else 0

    # set custom properties
    self.setCustomProperty("title", layerDef.title)
    self.setCustomProperty("credit", layerDef.credit)
    self.setCustomProperty("serviceUrl", layerDef.serviceUrl)
    self.setCustomProperty("yOriginTop", layerDef.yOriginTop)
    self.setCustomProperty("zmin", layerDef.zmin)
    self.setCustomProperty("zmax", layerDef.zmax)
    if layerDef.bbox:
      self.setCustomProperty("bbox", layerDef.bbox.toString())
    self.setCustomProperty("creditVisibility", self.creditVisibility)

    if pseudo_mercator is None:
      pseudo_mercator = QgsCoordinateReferenceSystem(3857)
    self.setCrs(pseudo_mercator)
    if layerDef.bbox:
      self.setExtent(BoundingBox.degreesToMercatorMeters(layerDef.bbox).toQgsRectangle())
    else:
      self.setExtent(QgsRectangle(-layerDef.TSIZE1, -layerDef.TSIZE1, layerDef.TSIZE1, layerDef.TSIZE1))
    self.setValid(True)
    self.tiles = None
    self.useLastZoomForPrint = False
    self.canvasLastZoom = 0
    self.setTransparency(LayerDefaultSettings.TRANSPARENCY)
    self.setBlendModeByName(LayerDefaultSettings.BLEND_MODE)
    self.setSmoothRender(LayerDefaultSettings.SMOOTH_RENDER)

    self.downloader = Downloader(self)
    self.downloader.userAgent = "QGIS/{0} TileLayerPlugin/{1}".format(QGis.QGIS_VERSION, self.plugin.VERSION) # not written since QGIS 2.2
    self.downloader.DEFAULT_CACHE_EXPIRATION = QSettings().value("/qgis/defaultTileExpiry", 24, type=int)
    QObject.connect(self.downloader, SIGNAL("replyFinished(QString, int, int)"), self.networkReplyFinished)

    # multi-thread rendering
    self.eventLoop = None
    QObject.connect(self, SIGNAL("fetchRequest(QStringList)"), self.fetchRequest)
    if self.iface:
      QObject.connect(self, SIGNAL("showMessage(QString, int)"), self.showStatusMessageSlot)
      QObject.connect(self, SIGNAL("showBarMessage(QString, QString, int, int)"), self.showBarMessageSlot)

  def setBlendModeByName(self, modeName):
    self.blendModeName = modeName
    blendMode = getattr(QPainter, "CompositionMode_" + modeName, 0)
    self.setBlendMode(blendMode)
    self.setCustomProperty("blendMode", modeName)

  def setTransparency(self, transparency):
    self.transparency = transparency
    self.setCustomProperty("transparency", transparency)

  def setSmoothRender(self, isSmooth):
    self.smoothRender = isSmooth
    self.setCustomProperty("smoothRender", 1 if isSmooth else 0)

  def setCreditVisibility(self, visible):
    self.creditVisibility = visible
    self.setCustomProperty("creditVisibility", 1 if visible else 0)

  def draw(self, renderContext):
    self.renderContext = renderContext
    if renderContext.extent().isEmpty():
      qDebug("Drawing is skipped because map extent is empty.")
      return True

    painter = renderContext.painter()
    if not self.isCurrentCrsSupported():
      if self.plugin.navigationMessagesEnabled:
        msg = self.tr("TileLayer is available in EPSG:3857")
        self.showBarMessage(msg, QgsMessageBar.INFO, 2)
      return True

    mapSettings = self.iface.mapCanvas().mapSettings() if self.plugin.apiChanged23 else self.iface.mapCanvas().mapRenderer()
    isDpiEqualToCanvas = renderContext.painter().device().logicalDpiX() == mapSettings.outputDpi()
    if isDpiEqualToCanvas or not self.useLastZoomForPrint:
      # calculate zoom level
      mpp1 = self.layerDef.TSIZE1 / self.layerDef.TILE_SIZE
      zoom = int(math.ceil(math.log(mpp1 / renderContext.mapToPixel().mapUnitsPerPixel(), 2) + 1))
      zoom = max(0, min(zoom, self.layerDef.zmax))
      #zoom = max(self.layerDef.zmin, zoom)
    else:
      # for print composer output image, use last zoom level of map item on print composer (or map canvas)
      zoom = self.canvasLastZoom

    # zoom limit
    if zoom < self.layerDef.zmin:
      if self.plugin.navigationMessagesEnabled:
        msg = self.tr("Current zoom level ({0}) is smaller than zmin ({1}): {2}").format(zoom, self.layerDef.zmin, self.layerDef.title)
        self.showBarMessage(msg, QgsMessageBar.INFO, 2)
      return True

    while True:
      # calculate tile range (yOrigin is top)
      size = self.layerDef.TSIZE1 / 2 ** (zoom - 1)
      matrixSize = 2 ** zoom
      ulx = max(0, int((renderContext.extent().xMinimum() + self.layerDef.TSIZE1) / size))
      uly = max(0, int((self.layerDef.TSIZE1 - renderContext.extent().yMaximum()) / size))
      lrx = min(int((renderContext.extent().xMaximum() + self.layerDef.TSIZE1) / size), matrixSize - 1)
      lry = min(int((self.layerDef.TSIZE1 - renderContext.extent().yMinimum()) / size), matrixSize - 1)

      # bounding box limit
      if self.layerDef.bbox:
        trange = self.layerDef.bboxDegreesToTileRange(zoom, self.layerDef.bbox)
        ulx = max(ulx, trange.xmin)
        uly = max(uly, trange.ymin)
        lrx = min(lrx, trange.xmax)
        lry = min(lry, trange.ymax)
        if lrx < ulx or lry < uly:
          # tile range is out of the bounding box
          return True

      # tile count limit
      tileCount = (lrx - ulx + 1) * (lry - uly + 1)
      if tileCount > self.MAX_TILE_COUNT:
        # as tile count is over the limit, decrease zoom level
        zoom -= 1

        # if the zoom level is less than the minimum, do not draw
        if zoom < self.layerDef.zmin:
          msg = self.tr("Tile count is over limit ({0}, max={1})").format(tileCount, self.MAX_TILE_COUNT)
          self.showBarMessage(msg, QgsMessageBar.WARNING, 4)
          return True
        continue

      # zoom level has been determined
      break

    # save painter state
    painter.save()

    pt = renderContext.mapToPixel().transform(renderContext.extent().xMaximum(), renderContext.extent().yMinimum())
    scaleX = pt.x() / painter.viewport().size().width()
    scaleY = pt.y() / painter.viewport().size().height()
    painter.scale(scaleX, scaleY)

    if debug_mode:
      self.logT("TileLayer.draw()")
      qDebug("Bottom-right of extent (pixel): %f, %f" % (pt.x(), pt.y()))   # Top-left is (0, 0)
      qDebug("Calculated scale: %f, %f" % (scaleX, scaleY))

    # set pen and font
    painter.setPen(Qt.black)
    font = QFont(painter.font())
    font.setPointSize(10)
    painter.setFont(font)

    if self.layerDef.serviceUrl[0] == ":":
      painter.setBrush(QBrush(Qt.NoBrush))
      self.drawDebugInfo(renderContext, zoom, ulx, uly, lrx, lry, 1.0 / scaleX, 1.0 / scaleY)
    else:
      # create Tiles class object and throw url into it
      tiles = Tiles(zoom, ulx, uly, lrx, lry, self.layerDef)
      urls = []
      cacheHits = 0
      for ty in range(uly, lry + 1):
        for tx in range(ulx, lrx + 1):
          data = None
          url = self.layerDef.tileUrl(zoom, tx, ty)
          if self.tiles and zoom == self.tiles.zoom and url in self.tiles.tiles:
            data = self.tiles.tiles[url].data
          tiles.addTile(url, Tile(zoom, tx, ty, data))
          if data is None:
            urls.append(url)
          elif data:      # memory cache exists
            cacheHits += 1
          #else:    # tile was not found (Downloader.NOT_FOUND=0)

      self.tiles = tiles
      if len(urls) > 0:
        # fetch tile data
        if self.plugin.apiChanged23:
          files = self.fetchFiles(urls)
        else:
          files = self.downloader.fetchFiles(urls, self.plugin.downloadTimeout)

        for url in files.keys():
          self.tiles.setImageData(url, files[url])

        if self.iface:
          cacheHits += self.downloader.cacheHits
          downloadedCount = self.downloader.fetchSuccesses - self.downloader.cacheHits
          msg = self.tr("{0} files downloaded. {1} caches hit.").format(downloadedCount, cacheHits)
          barmsg = None
          if self.downloader.errorStatus != Downloader.NO_ERROR:
            if self.downloader.errorStatus == Downloader.TIMEOUT_ERROR:
              barmsg = self.tr("Download Timeout - {}").format(self.name())
            else:
              msg += self.tr(" {} files failed.").format(self.downloader.fetchErrors)
              if self.downloader.fetchSuccesses == 0:
                barmsg = self.tr("Failed to download all {0} files. - {1}").format(self.downloader.fetchErrors, self.name())
          self.showStatusMessage(msg, 5000)
          if barmsg:
            self.showBarMessage(barmsg, QgsMessageBar.WARNING, 4)

      # apply layer style
      oldOpacity = painter.opacity()
      painter.setOpacity(0.01 * (100 - self.transparency))
      oldSmoothRenderHint = painter.testRenderHint(QPainter.SmoothPixmapTransform)
      if self.smoothRender:
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

      # draw tiles
      self.drawTiles(renderContext, self.tiles, 1.0 / scaleX, 1.0 / scaleY)
      #self.drawTilesDirectly(renderContext, self.tiles, 1.0 / scaleX, 1.0 / scaleY)

      # restore layer style
      painter.setOpacity(oldOpacity)
      if self.smoothRender:
        painter.setRenderHint(QPainter.SmoothPixmapTransform, oldSmoothRenderHint)

      # draw credit on the bottom right corner
      if self.creditVisibility and self.layerDef.credit:
        margin, paddingH, paddingV = (5, 4, 3)
        canvasSize = painter.viewport().size()
        rect = QRect(0, 0, canvasSize.width() - margin, canvasSize.height() - margin)
        textRect = painter.boundingRect(rect, Qt.AlignBottom | Qt.AlignRight, self.layerDef.credit)
        bgRect = QRect(textRect.left() - paddingH, textRect.top() - paddingV, textRect.width() + 2 * paddingH, textRect.height() + 2 * paddingV)
        painter.fillRect(bgRect, QColor(240, 240, 240, 150))  #197, 234, 243, 150))
        painter.drawText(rect, Qt.AlignBottom | Qt.AlignRight, self.layerDef.credit)

        if debug_mode:
          #painter.fillRect(rect, QColor(240, 240, 240, 200))
          qDebug("credit text rect: " + str(textRect))

    if 0: #debug_mode:
      # draw plugin icon
      image = QImage(os.path.join(os.path.dirname(QFile.decodeName(__file__)), "icon_old.png"))
      painter.drawImage(5, 5, image)
      self.logT("TileLayer.draw() ends")

    # restore painter state
    painter.restore()

    if isDpiEqualToCanvas:
      # save zoom level for printing (output with different dpi from map canvas)
      self.canvasLastZoom = zoom
    return True

  def drawTiles(self, renderContext, tiles, sdx=1.0, sdy=1.0):
    # create an image that has the same resolution as the tiles
    image = tiles.image()

    # tile extent to pixel
    map2pixel = renderContext.mapToPixel()
    extent = tiles.extent()
    topLeft = map2pixel.transform(extent.topLeft().x(), extent.topLeft().y())
    bottomRight = map2pixel.transform(extent.bottomRight().x(), extent.bottomRight().y())
    rect = QRect(QPoint(round(topLeft.x() * sdx), round(topLeft.y() * sdy)), QPoint(round(bottomRight.x() * sdx), round(bottomRight.y() * sdy)))

    # draw the image on the map canvas
    renderContext.painter().drawImage(rect, image)

    self.log("Tiles extent: " + str(extent))
    self.log("Draw into canvas rect: " + str(rect))

  def drawTilesDirectly(self, renderContext, tiles, sdx=1.0, sdy=1.0):
    p = renderContext.painter()
    for url, tile in tiles.tiles.items():
      self.log("Draw tile: zoom: %d, x:%d, y:%d, data:%s" % (tile.zoom, tile.x, tile.y, str(tile.data)))
      rect = self.getTileRect(renderContext, tile.zoom, tile.x, tile.y, sdx, sdy)
      if tile.data:
        image = QImage()
        image.loadFromData(tile.data)
        p.drawImage(rect, image)

  def drawDebugInfo(self, renderContext, zoom, ulx, uly, lrx, lry, sdx, sdy):
    if "frame" in self.layerDef.serviceUrl:
      self.drawFrames(renderContext, zoom, ulx, uly, lrx, lry, sdx, sdy)
    if "number" in self.layerDef.serviceUrl:
      self.drawNumbers(renderContext, zoom, ulx, uly, lrx, lry, sdx, sdy)
    if "info" in self.layerDef.serviceUrl:
      self.drawInfo(renderContext, zoom, ulx, uly, lrx, lry)

  def drawFrame(self, renderContext, zoom, x, y, sdx, sdy):
    rect = self.getTileRect(renderContext, zoom, x, y, sdx, sdy)
    p = renderContext.painter()
    #p.drawRect(rect)   # A slash appears on the top-right tile without Antialiasing render hint.
    pts = [rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft(), rect.topLeft()]
    for i in range(4):
      p.drawLine(pts[i], pts[i+1])

  def drawFrames(self, renderContext, zoom, xmin, ymin, xmax, ymax, sdx, sdy):
    for y in range(ymin, ymax + 1):
      for x in range(xmin, xmax + 1):
        self.drawFrame(renderContext, zoom, x, y, sdx, sdy)

  def drawNumber(self, renderContext, zoom, x, y, sdx, sdy):
    rect = self.getTileRect(renderContext, zoom, x, y, sdx, sdy)
    p = renderContext.painter()
    if not self.layerDef.yOriginTop:
      y = (2 ** zoom - 1) - y
    p.drawText(rect, Qt.AlignCenter, "(%d, %d)\nzoom: %d" % (x, y, zoom));

  def drawNumbers(self, renderContext, zoom, xmin, ymin, xmax, ymax, sdx, sdy):
    for y in range(ymin, ymax + 1):
      for x in range(xmin, xmax + 1):
        self.drawNumber(renderContext, zoom, x, y, sdx, sdy)

  def drawInfo(self, renderContext, zoom, xmin, ymin, xmax, ymax):
    mapSettings = self.iface.mapCanvas().mapSettings() if self.plugin.apiChanged23 else self.iface.mapCanvas().mapRenderer()
    lines = []
    lines.append("TileLayer")
    lines.append(" zoom: %d, tile matrix extent: (%d, %d) - (%d, %d), tile count: %d * %d" % (zoom, xmin, ymin, xmax, ymax, xmax - xmin, ymax - ymin) )
    lines.append(" map extent: %s" % renderContext.extent().toString() )
    lines.append(" map center: %lf, %lf" % (renderContext.extent().center().x(), renderContext.extent().center().y() ) )
    lines.append(" map size: %f, %f" % (renderContext.extent().width(), renderContext.extent().height() ) )
    lines.append(" canvas size (pixel): %d, %d" % (renderContext.painter().viewport().size().width(), renderContext.painter().viewport().size().height() ) )
    lines.append(" logicalDpiX: %f" % renderContext.painter().device().logicalDpiX() )
    lines.append(" outputDpi: %f" % mapSettings.outputDpi() )
    lines.append(" mapToPixel: %s" % renderContext.mapToPixel().showParameters() )
    p = renderContext.painter()
    textRect = p.boundingRect(QRect(QPoint(0, 0), p.viewport().size()), Qt.AlignLeft, "Q")
    for i, line in enumerate(lines):
      p.drawText(10, (i + 1) * textRect.height(), line)
      self.log(line)

  def getTileRect(self, renderContext, zoom, x, y, sdx=1.0, sdy=1.0):
    """ get tile pixel rect in the render context """
    r = self.layerDef.getTileRect(zoom, x, y)
    map2pix = renderContext.mapToPixel()
    topLeft = map2pix.transform(r.xMinimum(), r.yMaximum())
    bottomRight = map2pix.transform(r.xMaximum(), r.yMinimum())
    return QRect(QPoint(round(topLeft.x() * sdx), round(topLeft.y() * sdy)), QPoint(round(bottomRight.x() * sdx), round(bottomRight.y() * sdy)))
    #return QRectF(QPointF(round(topLeft.x()), round(topLeft.y())), QPointF(round(bottomRight.x()), round(bottomRight.y())))
    #return QgsRectangle(topLeft, bottomRight)

  def isCurrentCrsSupported(self):
    mapSettings = self.iface.mapCanvas().mapSettings() if self.plugin.apiChanged23 else self.iface.mapCanvas().mapRenderer()
    return mapSettings.destinationCrs().postgisSrid() == 3857

  def networkReplyFinished(self, url, error, isFromCache):
    if self.iface is None or isFromCache:
      return
    unfinishedCount = self.downloader.unfinishedCount()
    if unfinishedCount == 0:
      self.emit(SIGNAL("allRepliesFinished()"))

    downloadedCount = self.downloader.fetchSuccesses - self.downloader.cacheHits
    totalCount = self.downloader.finishedCount() + unfinishedCount
    msg = self.tr("{0} of {1} files downloaded.").format(downloadedCount, totalCount)
    if self.downloader.fetchErrors:
      msg += self.tr(" {} files failed.").format(self.downloader.fetchErrors)
    self.showStatusMessage(msg)

  def readXml(self, node):
    self.readCustomProperties(node)
    self.layerDef.title = self.customProperty("title", "")
    self.layerDef.credit = self.customProperty("credit", "")
    if self.layerDef.credit == "":
      self.layerDef.credit = self.customProperty("providerName", "")    # for compatibility with 0.11
    self.layerDef.serviceUrl = self.customProperty("serviceUrl", "")
    self.layerDef.yOriginTop = int(self.customProperty("yOriginTop", 1))
    self.layerDef.zmin = int(self.customProperty("zmin", TileDefaultSettings.ZMIN))
    self.layerDef.zmax = int(self.customProperty("zmax", TileDefaultSettings.ZMAX))
    bbox = self.customProperty("bbox", None)
    if bbox:
      self.layerDef.bbox = BoundingBox.fromString(bbox)
      self.setExtent(BoundingBox.degreesToMercatorMeters(self.layerDef.bbox).toQgsRectangle())
    # layer style
    self.setTransparency(int(self.customProperty("transparency", LayerDefaultSettings.TRANSPARENCY)))
    self.setBlendModeByName(self.customProperty("blendMode", LayerDefaultSettings.BLEND_MODE))
    self.setSmoothRender(int(self.customProperty("smoothRender", LayerDefaultSettings.SMOOTH_RENDER)))
    self.creditVisibility = int(self.customProperty("creditVisibility", 1))
    return True

  def writeXml(self, node, doc):
    element = node.toElement();
    element.setAttribute("type", "plugin")
    element.setAttribute("name", TileLayer.LAYER_TYPE);
    return True

  def metadata(self):
    lines = []
    fmt = u"%s:\t%s"
    lines.append(fmt % (self.tr("Title"), self.layerDef.title))
    lines.append(fmt % (self.tr("Credit"), self.layerDef.credit))
    lines.append(fmt % (self.tr("URL"), self.layerDef.serviceUrl))
    lines.append(fmt % (self.tr("yOrigin"), u"%s (yOriginTop=%d)" % (("Bottom", "Top")[self.layerDef.yOriginTop], self.layerDef.yOriginTop)))
    if self.layerDef.bbox:
      extent = self.layerDef.bbox.toString()
    else:
      extent = self.tr("Not set")
    lines.append(fmt % (self.tr("Zoom range"), "%d - %d" % (self.layerDef.zmin, self.layerDef.zmax)))
    lines.append(fmt % (self.tr("Layer Extent"), extent))
    return "\n".join(lines)

  def log(self, msg):
    if debug_mode:
      qDebug(msg)

  def logT(self, msg):
    if debug_mode:
      qDebug("%s: %s" % (str(threading.current_thread()), msg))

  def dump(self, detail=False, bbox=None):
    pass

  # functions for multi-thread rendering
  def fetchFiles(self, urls):
    self.logT("TileLayer.fetchFiles() starts")
    # create a QEventLoop object that belongs to the current thread (if ver. > 2.1, it is render thread)
    eventLoop = QEventLoop()
    self.logT("Create event loop: " + str(eventLoop))    #DEBUG
    QObject.connect(self, SIGNAL("allRepliesFinished()"), eventLoop.quit)

    # create a timer to watch whether rendering is stopped
    watchTimer = QTimer()
    watchTimer.timeout.connect(eventLoop.quit)

    # send a fetch request to the main thread
    self.emit(SIGNAL("fetchRequest(QStringList)"), urls)

    # wait for the fetch to finish
    tick = 0
    interval = 500
    timeoutTick = self.plugin.downloadTimeout * 1000 / interval
    watchTimer.start(interval)
    while tick < timeoutTick:
      # run event loop for 0.5 seconds at maximum
      eventLoop.exec_()

      if debug_mode:
        qDebug("watchTimerTick: %d" % tick)
        qDebug("unfinished downloads: %d" % self.downloader.unfinishedCount())

      if self.downloader.unfinishedCount() == 0 or self.renderContext.renderingStopped():
        break
      tick += 1
    watchTimer.stop()

    if tick == timeoutTick and self.downloader.unfinishedCount() > 0:
      self.log("fetchFiles timeout")
      self.showBarMessage("fetchFiles timeout", duration=5)   #DEBUG
      self.downloader.abort()
      self.downloader.errorStatus = Downloader.TIMEOUT_ERROR
    files = self.downloader.fetchedFiles

    watchTimer.timeout.disconnect(eventLoop.quit)   #
    QObject.disconnect(self, SIGNAL("allRepliesFinished()"), eventLoop.quit)

    self.logT("TileLayer.fetchFiles() ends")
    return files

  def fetchRequest(self, urls):
    self.logT("TileLayer.fetchRequest()")
    self.downloader.fetchFilesAsync(urls, self.plugin.downloadTimeout)

  def showStatusMessage(self, msg, timeout=0):
    self.emit(SIGNAL("showMessage(QString, int)"), msg, timeout)

  def showStatusMessageSlot(self, msg, timeout):
    self.iface.mainWindow().statusBar().showMessage(msg, timeout)

  def showBarMessage(self, text, level=QgsMessageBar.INFO, duration=0, title=None):
    if title is None:
      title = self.plugin.pluginName
    self.emit(SIGNAL("showBarMessage(QString, QString, int, int)"), title, text, level, duration)

  def showBarMessageSlot(self, title, text, level, duration):
    self.iface.messageBar().pushMessage(title, text, level, duration)

#  def createMapRenderer(self, renderContext):
#    qDebug("createMapRenderer")
#    self.renderer = QgsPluginLayerRenderer(self, renderContext)
#    return self.renderer

class TileLayerType(QgsPluginLayerType):
  def __init__(self, plugin):
    QgsPluginLayerType.__init__(self, TileLayer.LAYER_TYPE)
    self.plugin = plugin

  def createLayer(self):
    return TileLayer(self.plugin, TileServiceInfo.createEmptyInfo())

  def showLayerProperties(self, layer):
    from propertiesdialog import PropertiesDialog
    dialog = PropertiesDialog(layer)
    dialog.show()
    accepted = dialog.exec_()
    if accepted:
      layer.setTransparency(dialog.ui.spinBox_Transparency.value())
      layer.setBlendModeByName(dialog.ui.comboBox_BlendingMode.currentText())
      layer.setSmoothRender(dialog.ui.checkBox_SmoothRender.isChecked())
      layer.setCreditVisibility(dialog.ui.checkBox_CreditVisibility.isChecked())
      layer.emit(SIGNAL("repaintRequested()"))
    return True
