# encoding: UTF-8

'''
本文件中实现了行情数据记录引擎，用于汇总TICK数据，并生成K线插入数据库。

使用DR_setting.json来配置需要收集的合约，以及主力合约代码。
'''

import json
import os
import copy
from collections import OrderedDict
from datetime import datetime, timedelta

from eventEngine import *
from vtGateway import VtSubscribeReq, VtLogData
from drBase import *


########################################################################
class DrEngine(object):
    """数据记录引擎"""
    
    settingFileName = 'DR_setting.json'
    settingFileName = os.getcwd() + '/dataRecorder/' + settingFileName

    #----------------------------------------------------------------------
    def __init__(self, mainEngine, eventEngine):
        """Constructor"""
        self.mainEngine = mainEngine
        self.eventEngine = eventEngine
        
        # 当前日期
        self.today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 主力合约代码映射字典，key为具体的合约代码（如IF1604），value为主力合约代码（如IF0000）
        self.activeSymbolDict = {}
        
        # Tick对象字典
        self.tickDict = {}
        
        # K线对象字典
        self.barDict = {}
        
        #日K对象字典
        self.dayBarDict = {}
        #日结队列，记录已经完成日结的日期
        self.dayEndQueue = []
        #日结启动状态,收到tick时启动日结，日结完成后关闭日结
        self.dayEndEnabled = False
        
        # 载入设置，订阅行情
        self.loadSetting()
        
    #----------------------------------------------------------------------
    def loadSetting(self):
        """载入设置"""
        with open(self.settingFileName) as f:
            setting = json.load(f)
            
            # 如果working设为False则不启动行情记录功能
            working = setting['working']
            if not working:
                return
            
            if 'tick' in setting:
                l = setting['tick']
                
                for symbol, gatewayName in l:
                    drTick = DrTickData()           # 该tick实例可以用于缓存部分数据（目前未使用）
                    self.tickDict[symbol] = drTick
                    
                    req = VtSubscribeReq()
                    req.symbol = symbol
                    self.mainEngine.subscribe(req, gatewayName)
                    
            if 'bar' in setting:
                l = setting['bar']
                
                for symbol, gatewayName in l:
                    bar = DrBarData()
                    self.barDict[symbol] = bar
                    dayBar=DrBarData()
                    self.dayBarDict[symbol]=dayBar
                    req = VtSubscribeReq()
                    req.symbol = symbol
                    self.mainEngine.subscribe(req, gatewayName)      
                    
            if 'active' in setting:
                d = setting['active']
                
                for activeSymbol, symbol in d.items():
                    self.activeSymbolDict[symbol] = activeSymbol
                    
            # 注册事件监听
            self.registerEvent()            

    #----------------------------------------------------------------------
    def procecssTickEvent(self, event):
        """处理行情推送"""
        tick = event.dict_['data']
        vtSymbol = tick.vtSymbol
        #检查tick是否在交易时间内
        min1=int(tick.time[:2]+tick.time[3:5])
        if (min1>CTP_TRADE_DAY_BEGIN and min1<CTP_TRADE_DAY_END) or min1>CTP_TRADE_NIGHT_BEGIN or min1<CTP_TRADE_NIGHT_END :
            #忽略交易量为0的无效数据
            if tick.volume>0 and tick.date!='' and tick.date!=None:
                #收到TICK时，注册日结事件，启用日结状态
                if not self.dayEndEnabled :
                    self.eventEngine.register(EVENT_TIMER,self.dayEnd)
                    self.dayEndEnabled = True
                # 转化Tick格式
                drTick = DrTickData()
                d = drTick.__dict__
                for key in d.keys():
                    if key != 'datetime':
                        d[key] = tick.__getattribute__(key)
                drTick.datetime = datetime.strptime(' '.join([tick.date, tick.time]), '%Y%m%d %H:%M:%S.%f')            
                
                # 更新Tick数据
                if vtSymbol in self.tickDict:
                    self.insertData(TICK_DB_NAME, vtSymbol, drTick)
                    
                    #将最新TICK更新入全局字典，用于日结更新日线数据
                    self.tickDict[vtSymbol]=drtick
                    
                    if vtSymbol in self.activeSymbolDict:
                        activeSymbol = self.activeSymbolDict[vtSymbol]
                        self.insertData(TICK_DB_NAME, activeSymbol, drTick)
                    
                    # 发出日志
                    self.writeDrLog(u'记录Tick数据%s，时间:%s, last:%s, bid:%s, ask:%s' 
                                    %(drTick.vtSymbol, drTick.time, drTick.lastPrice, drTick.bidPrice1, drTick.askPrice1))
                    
                # 更新分钟线数据
                if vtSymbol in self.barDict:
                    bar = self.barDict[vtSymbol]
                    
                    # 如果第一个TICK或者新的一分钟
                    if not bar.datetime or bar.datetime.minute != drTick.datetime.minute:    
                        if bar.vtSymbol:
                            newBar = copy.copy(bar)
                            self.insertData(MINUTE_DB_NAME, vtSymbol, newBar)
                            
                            if vtSymbol in self.activeSymbolDict:
                                activeSymbol = self.activeSymbolDict[vtSymbol]
                                self.insertData(MINUTE_DB_NAME, activeSymbol, newBar)                    
                            
                            self.writeDrLog(u'记录分钟线数据%s，时间:%s, O:%s, H:%s, L:%s, C:%s' 
                                            %(bar.vtSymbol, bar.time, bar.open, bar.high, 
                                              bar.low, bar.close))
                                 
                        bar.vtSymbol = drTick.vtSymbol
                        bar.symbol = drTick.symbol
                        bar.exchange = drTick.exchange
                        
                        bar.open = drTick.lastPrice
                        bar.high = drTick.lastPrice
                        bar.low = drTick.lastPrice
                        bar.close = drTick.lastPrice
                        
                        bar.date = drTick.date
                        bar.time = drTick.time
                        bar.datetime = drTick.datetime
                        bar.volume = drTick.volume
                        bar.openInterest = drTick.openInterest        
                    # 否则继续累加新的K线
                    else:                               
                        bar.high = max(bar.high, drTick.lastPrice)
                        bar.low = min(bar.low, drTick.lastPrice)
                        bar.close = drTick.lastPrice            
    #----------------------------------------------------------------------
    def dayEnd(self,event):
        """日结任务"""
        curTime=int(datetime.now().strftime("%H%M"))
        if  curTime>1600 and curTime < 1700 and self.dayEndEnabled:
        
            for vtSymbol in self.tickDict:
                ctick = DrTickData()
                dayBar = DrBarData()
                ctick = self.tickDict[vtSymbol]
                dayBar = self.dayBarDict[vtSymbol]
                dayBar.vtSymbol = ctick.vtSymbol        # vt系统代码
                dayBar.symbol = ctick.symbol          # 代码
                dayBar.exchange = ctick.exchange        # 交易所
            
                dayBar.open = ctick.openPrice             # OHLC
                dayBar.high = ctick.highPrice
                dayBar.low = ctick.lowPrice
                dayBar.close = ctick.lastPrice
                
                dayBar.date = ctick.date            # 日期
                dayBar.time = EMPTY_STRING            # 时间
                dayBar.datetime = datetime.strptime(ctick.date, '%Y%m%d')  # python的datetime时间对象
                
                dayBar.volume = ctick.volume             # 成交量
                dayBar.openInterest = ctick.openInterest      # 持仓量

                self.insertData(DAILY_DB_NAME, vtSymbol, dayBar)
            self.dayEndQueue.append(dayBar.date)
            self.dayEndEnabled = False
            self.eventEngine.unregister(EVENT_TIMER,self.dayEnd)
    #----------------------------------------------------------------------
    def registerEvent(self):
        """注册事件监听"""
        self.eventEngine.register(EVENT_TICK, self.procecssTickEvent)
        
 
    #----------------------------------------------------------------------
    def insertData(self, dbName, collectionName, data):
        """插入数据到数据库（这里的data可以是CtaTickData或者CtaBarData）"""
        self.mainEngine.dbInsert(dbName, collectionName, data.__dict__)
        
    #----------------------------------------------------------------------
    def writeDrLog(self, content):
        """快速发出日志事件"""
        log = VtLogData()
        log.logContent = content
        event = Event(type_=EVENT_DATARECORDER_LOG)
        event.dict_['data'] = log
        self.eventEngine.put(event)   
    