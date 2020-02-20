# -*- coding: utf-8 -*-

from abc import abstractmethod
import os
import c4d
from c4d import gui, plugins, bitmaps
import re
import time
from ctypes import pythonapi, c_void_p, py_object

#########################################
# Coded by: https://github.com/youdiaozi
# Use to: Decrypt c4d xpresso nodes, show them in original style, including those under protection.
# Usage: See README.md
#########################################

"""
高亮：
    PrivatePyCObjectAsVoidPtr
    SpecialEventAdd
    EventAdd - 界面更新
    CoreMessage - C4DThread - 线程和窗体消息交互
    GeDialog    
    CommandData
    GetPortDescID - re - 正则表达式

不处理：
    1 固有的port，如Math.input，不作处理（会使target的port看起来多了，而且python类可能类型对不上）。
        ——不作处理。可尝试node.SetPortType()解决?
    2 达到ports.show names的效果
        ——cpp可以实现，需要R21，参见pyext
    3 XPresso里的Point.GV_POINT_INPUT_OBJECT，访问出错。
        ——可能因为在desc里无法操作。不影响连接，可忽略。
    4 无法区分Cloner的部分同名desc(DESC_NAME)，参见self._nodeInfoStream.GetPortDescID()
        - P: MG_LINEAR_OBJECT_POSITION vs ID_MG_TRANSFORM_POSITION
        - S: MG_LINEAR_OBJECT_SCALE vs ID_MG_TRANSFORM_SCALE
        - R: MG_LINEAR_OBJECT_ROTATION vs ID_MG_TRANSFORM_ROTATE
    5 根XGroup，即root，无法获取bcd = bsc.GetContainerInstance(c4d.ID_OPERATORCONTAINER)，所以root的缩放、位置等不标准。
        - 参见NodeInfoStream.SetPosSize()
        
特点：
    1 采用非严格匹配，即SubID不一致，按出现的次序判断。
    2 赋值时，遇到同MainID而不同SubID的，需要对该id(即paramid[0].id)进行重新赋值，因为key不一样。参见：NodeInfoStream.ConvertPortSubIDList
    3 通过借用窗体，支持异步线程操作。
    
PortID生成特点：
    1 对于固有port，如Bitmap.X，Math.input等，不论是不是可重复的，MainID相同，SubID不同。
    2 对于非固有port，如Object.Desc和Object.UserData，MainID不同，SubID不同。
"""

# <editor-fold desc="插件变量">
PLUGIN_ID = 1054519
# </editor-fold>

# <editor-fold desc="全局变量">

# indentString = ">" * 4
indentString = " " * 4
levelString = "|---"

# operatorID（代替）
c4d_ID_OPERATOR_XGROUP = 1001144
c4d_ID_OPERATOR_PYTHON = 1022471

c4d_ID_OBJECT_PROPERTY_IN_FIRST = 10000000  # Inport
c4d_ID_OBJECT_PROPERTY_OUT_FIRST = 20000000  # Outport
"""
SPEC包括以下，不能用Object Properties即GetDescription获取的：
    In:
        Local Matrix 30000000
        Global Matrix 30000001
        History level 30000002
        Object 30000003
        On 30000004

    Out:
        Local Matrix 40000000
        Global Matrix 40000001
        Object 40000002
        Previous position 40000003
        Previous rotation 40000004
        Previous scale 40000005
        Previous global matrix 40000006
        Previous local matrix 40000007
        Position velocity 40000008
        Rotation velocity 40000009
        Scale velocity 40000010
"""
c4d_ID_OBJECT_PROPERTY_SPEC_IN_FIRST = 30000000
c4d_ID_OBJECT_PROPERTY_SPEC_OUT_FIRST = 40000000

# # 对应上面的 c4d_ID_OBJECT_PROPERTY_SPEC_IN_FIRST
# dictObjectPropertySpecIn = {"Local Matrix": 30000000, "Global Matrix": 30000001, "History level": 30000002,
#                             "Object": 30000003, "On": 30000004}
# # 对应上面的 c4d_ID_OBJECT_PROPERTY_SPEC_OUT_FIRST
# dictObjectPropertySpecOut = {"Local Matrix": 40000000, "Global Matrix": 40000001, "Object": 40000002,
#                              "Previous position": 40000003,
#                              "Previous rotation": 40000004, "Previous scale": 40000005,
#                              "Previous global matrix": 40000006,
#                              "Previous local matrix": 40000007, "Position velocity": 40000008,
#                              "Rotation velocity": 40000009,
#                              "Scale velocity": 40000010}
dictIO = {c4d.GV_PORT_INVALID: "[INVALID]", c4d.GV_PORT_INPUT: "[IN ]", c4d.GV_PORT_OUTPUT: "[OUT]",
          c4d.GV_PORT_INPUT_OR_GEDATA: "[GEDATA]"}

codePython = '''
import c4d
#Welcome to the world of Python

def UnHideObject(obj):
    obj.ChangeNBit(c4d.NBIT_THIDE,c4d.NBITCONTROL_CLEAR)
    obj.ChangeNBit(c4d.NBIT_OHIDE,c4d.NBITCONTROL_CLEAR)

def UnlockAllLayers():
    doc = c4d.documents.GetActiveDocument()
    if not doc: return

    # print "=== UnlockAllLayers ==="
    layerRoot = doc.GetLayerObjectRoot()
    if layerRoot is not None:
        layer = layerRoot.GetDown()
        while layer is not None:
            layer[1000] = 0 # solo
            layer[1003] = 1 # visible in manager
            layer[1004] = 0 # locked
            layer[c4d.ID_LAYER_VIEW] = 1
            layer[c4d.ID_LAYER_RENDER] = 1

            layer = layer.GetNext()

def Unlock(obj):
    UnlockAllLayers()

    if obj is not None:
        # print "++++++++++++++++++++++++++++++"
        # print "Object is:", obj
        for tag2 in obj.GetTags():
            # print "|---", tag2[c4d.ID_BASELIST_NAME], tag2.GetType()
            UnHideObject(tag2)
            if tag2.GetType() == c4d.Texpresso or tag2.GetType() == c4d.Tpython:
                tag2[c4d.EXPRESSION_ENABLE] = False

        for child in obj.GetChildren():
            Unlock(child)

def main():
    #obj = doc.SearchObject("Unlocker")
    obj = op.GetObject()[c4d.ID_USERDATA,1]
    Unlock(obj)
'''

# </editor-fold>

class Util:
    @staticmethod
    def UnHideObject(obj):
        obj.ChangeNBit(c4d.NBIT_THIDE, c4d.NBITCONTROL_CLEAR)
        obj.ChangeNBit(c4d.NBIT_OHIDE, c4d.NBITCONTROL_CLEAR)

    @staticmethod
    def GetDictValue3(dict, valueTarget):
        if dict is None or valueTarget is None:
            return None
    
        for key, value in dict.items():
            if value[0].node == valueTarget:
                return value[1].node
    
        return None

    @staticmethod
    def GetLeadingString(hi):
        leadingString = ""
        if hi != 0:
            leadingString = indentString * (hi - 1) + levelString
    
        return leadingString

    @staticmethod
    def NewLine(printFlag=True):
        if printFlag: print(" \n")
    
    """
    @没有使用，保留
    obj可以是object，也可以是tag
    GetDescription无法获取只在xpresso里显示的port，因为description加入到port后，id变了
    
    paramid[0].dtype: c4d.DTYPE_GROUP，即为XGroup
    paramid[0].id: id值，=c4d.ID_BASELIST_NAME    
    bc[c4d.DESC_IDENT]: "ID_BASELIST_NAME"，即id的原名称
    """
    @staticmethod
    def PrintAllDescriptions(obj):
        # DESCFLAGS_DESC_NONE查询不到，应该为DESCFLAGS_DESC_0
        description = obj.GetDescription(c4d.DESCFLAGS_DESC_0)  # Get the description of the active object
        print "=========================="
        for bc, paramid, groupid in description:  # Iterate over the parameters of the description
            isGroup = paramid[0].dtype == c4d.DTYPE_GROUP  # 判断是不是分组，即XGroup
            print obj[c4d.ID_BASELIST_NAME] + "." + bc[c4d.DESC_NAME], "-", bc[c4d.DESC_IDENT], " (",  isGroup, ")"
            print indentString, "|---paramid:", paramid  # paramid是DescID，paramid[0].id是DescLevel对象，即key
            print indentString, "|---groupid:", groupid
    
    """
    其实一定是None，因为obj是新生成的。暂时保留not None的代码
    """
    @staticmethod
    def GetOrCreateTag(obj, tagType, tagIndex):
        # print("obj = {}".format(obj))
        tag = obj.GetTag(tagType, tagIndex)
        # print("tag = {} @ {}".format(tag, tagIndex))
        if tag is None:
            tag = c4d.BaseTag(tagType)
            obj.InsertTag(tag, obj.GetLastTag())
            return tag, len(obj.GetTags()) - 1
        # else:
        #     for index, tag2 in enumerate(obj.GetTags()):
        #         if tag2 == tag:
        #             return tag, index
    
        return None, -1

class NodeInfo(object):
    # TODO 重要：私有变量可以直接在__init__等里面定义。而不能在这里定义_px = 0.0，因为这样的话，变量会被当成全局变量，而不是类的私有变量。
    # _px = 0.0 # ERROR

    def __init__(self, node):
        self._node = node
        self._operatorID = self._node.GetOperatorID()

        self._children = []
        self._px = 0.0
        self._py = 0.0
        self._sx = 0.0
        self._sy = 0.0

        self._pxGroup = 0.0
        self._pyGroup = 0.0
        self._zoomGroup = 1.0
  
    # <editor-fold desc="NodeInfo固定属性、方法">

    """
    子类操作：添加RefObject，仅适用于ObjectNodeInfo
    """
    def SetSpecItems(self, sourceNodeInfo):
        pass

    """
    通用显示名
    子类操作时，主要是Math和FloatMath
    """
    def GetDisplayName(self):
        name = self._node[c4d.ID_BASELIST_NAME]
        return name

    def __repr__(self):
        name = self[c4d.ID_BASELIST_NAME]
        count = len(self.GetChildren())
        return "{}({}) @ [{},{}], size[{},{}] - count: {}".format(name, self.operatorID, int(self.px), int(self.py),
                                                                  int(self.sx), int(self.sy), count)

    def __getitem__(self, key):
        return self._node[key]

    def __setitem__(self, key, value):
        self._node[key] = value

    def GetChildren(self):
        return self._children

    """
    len()
    """
    def __len__(self):
        return len(self.GetChildren())

    """
    bool()，用于判断，如if nodeInfo:
    ——似乎没有用，还是要用if nodeInfo is not None:
    """
    def __bool__(self):
        return True

    @property
    def node(self):
        return self._node

    @property
    def px(self):
        return self._px

    @px.setter
    def px(self, value):
        self._px = value

    @property
    def py(self):
        return self._py

    @py.setter
    def py(self, value):
        self._py = value

    @property
    def sx(self):
        return self._sx

    @sx.setter
    def sx(self, value):
        self._sx = value

    @property
    def sy(self):
        return self._sy

    @sy.setter
    def sy(self, value):
        self._sy = value
        
    @property
    def pxGroup(self):
        return self._pxGroup

    @pxGroup.setter
    def pxGroup(self, value):
        self._pxGroup = value

    @property
    def pyGroup(self):
        return self._pyGroup

    @pyGroup.setter
    def pyGroup(self, value):
        self._pyGroup = value

    @property
    def zoomGroup(self):
        return self._zoomGroup

    @zoomGroup.setter
    def zoomGroup(self, value):
        self._zoomGroup = value

    @property
    def operatorID(self):
        return self._operatorID

    # </editor-fold>

# <editor-fold desc="NodeInfo的继承子类">

class ObjectNodeInfo(NodeInfo):
    def SetSpecItems(self, sourceNodeInfo):
        try:
            self[c4d.GV_OBJECT_OBJECT_ID] = sourceNodeInfo[c4d.GV_OBJECT_OBJECT_ID]
        except Exception, e:
            print("[ERROR][4]{}".format(e))

    def GetDisplayName(self):
        name = ""
        try:
            refObject = self.node[c4d.GV_OBJECT_OBJECT_ID]
            if refObject:
                name = refObject[c4d.ID_BASELIST_NAME]  # + "(object)"
        except:
            name = super(ObjectNodeInfo, self).GetDisplayName()  # super后面跟的是自己的类名，而不是父类名

        return name

class MathNodeInfo(NodeInfo):
    def SetSpecItems(self, sourceNodeInfo):
        try:
            self[c4d.GV_MATH_FUNCTION_ID] = sourceNodeInfo[c4d.GV_MATH_FUNCTION_ID]
        except Exception, e:
            print("[ERROR][5]{}".format(e))

    def GetDisplayName(self):
        dict = {0: "Add", 1: "Subtract", 2: "Multiply", 3: "Divide", 4: "Modulo"}
        key = self.node[c4d.GV_MATH_FUNCTION_ID]

        name = super(MathNodeInfo, self).GetDisplayName()
        if dict.has_key(key):
            name += ":" + dict[key]

        return name

class FloatMathNodeInfo(NodeInfo):
    def SetSpecItems(self, sourceNodeInfo):
        try:
            self[c4d.GV_FLOATMATH_FUNCTION_ID] = sourceNodeInfo[c4d.GV_FLOATMATH_FUNCTION_ID]
        except Exception, e:
            print("[ERROR][6]{}".format(e))

    def GetDisplayName(self):
        dict = {0: "Add", 1: "Subtract", 2: "Multiply", 3: "Divide"}
        key = self.node[c4d.GV_FLOATMATH_FUNCTION_ID]

        name = super(FloatMathNodeInfo, self).GetDisplayName()
        if dict.has_key(key):
            name += ":" + dict[key]

        return name

# </editor-fold>

class NodeInfoStream(object):
    def __init__(self, workThread):
        self._workThread = workThread
        # Source -> Target
        self.dictNodeInfoMapping = {}
        self.dictPortMapping = {}

    """
    类内调用同线程thread，更新信息。
    """
    def UpdateInfoText(self, infoText=""):
        self._workThread.UpdateInfoText(infoText) # NodeInfoStream内调用，更新信息

    def SetPosSize(self, nodeInfoSource, nodeInfoTarget):
        # 设置位置、大小
        # GetDataInstance, GetContainerInstance
        bc = nodeInfoTarget.node.GetDataInstance()  # Get copy of base container
        print("nodeInfoTarget:{} - {}".format(nodeInfoTarget[c4d.ID_BASELIST_NAME], bc))
        if bc is not None:
            # print "---BC"
            bsc = bc.GetContainerInstance(c4d.ID_SHAPECONTAINER)  # Get copy of shape container
            if bsc is not None:
                # print "---BSC"
                bcd = bsc.GetContainerInstance(c4d.ID_OPERATORCONTAINER)  # Get copy of operator container
                if bcd is not None:
                    # print "---BCD OK"
                    # 位置
                    bcd.SetReal(100, nodeInfoSource.px)  # Set x position
                    bcd.SetReal(101, nodeInfoSource.py)  # Set y position

                    # 大小：XGroup特殊处理
                    if nodeInfoTarget.node.IsGroupNode():
                        bcd.SetReal(110, nodeInfoSource.sx)  # Set XGroup x scale
                        bcd.SetReal(111, nodeInfoSource.sy)  # Set XGroup y scale
                        bcd.SetReal(102, nodeInfoSource.pxGroup) # xgroup inner xpos
                        bcd.SetReal(103, nodeInfoSource.pyGroup) # xgroup inner ypos
                        bcd.SetReal(104, nodeInfoSource.zoomGroup) # xgroup inner zoom
                        # print("nodeInfo2:{},{} - {}".format(nodeInfoSource.pxGroup, nodeInfoSource.pyGroup, nodeInfoSource.zoomGroup))
                    else:
                        bcd.SetReal(108, nodeInfoSource.sx)  # Set x scale
                        bcd.SetReal(109, nodeInfoSource.sy)  # Set y scale

                    # print("{}SetPosSize:{} - [{},{}] @ [{},{}]".format(Util.GetLeadingString(1), nodeInfoTarget.node[c4d.ID_BASELIST_NAME],
                    #                                                    nodeInfoSource.px, nodeInfoSource.py,
                    #                                                    nodeInfoSource.sx, nodeInfoSource.sy))
                    return True

        return False

    """
        ID_OPERATORCONTAINER
            坐标以右下为正
            特别注意：
                根XGroup，即root，无法获取bcd = bsc.GetContainerInstance(c4d.ID_OPERATORCONTAINER)，所以root的缩放、位置等不标准。 
            key normal / XGroup
            118 0 / 0
            105 View(0=Minimized, 1=Standard/Locked, 2=Extended)
                - 任何状态下，点Locked会自动切换到Standard状态
            119 View上一个状态
            104 1.0 / Zoom(右上角：zoom anchor，或者右键Zoom 100%，同一个数值)
            102 0.0 / xgroup inner xpos(右上角：pan anchor)
            103 0.0 / xgroup inner ypos(右上角：pan anchor)
            106 / 同[108]
            107 / [109]/2
            108 xsize / preference xgroup xsize(默认值：99.0，Optimize：150.0)
                - preference xgroup xsize，并不是Optimize之后，xsize就会变成这个值，这是跟普通的node不一样的地方。
            109 ysize / preference xgroup ysize(默认值：36.0，Optimize：36.0)
                - preference xgroup xsize，并不是Optimize之后，xsize就会变成这个值，这是跟普通的node不一样的地方。
            110 preference xsize / xgroup xsize
            111 preference ysize / xgroup ysize
            112 16.0 / 16.0
            113 12.0 / 12.0
            114 28.0 / 28.0
            115 30.0 / 30.0
            116 28.0 / 28.0
            117 30.0 / 30.0
            100 xpos
            101 ypos
        """
    def CalcPosSize(self, nodeInfo):
        # 设置位置、大小
        bc = nodeInfo.node.GetData()  # Get copy of base container
        if bc is not None:
            bsc = bc.GetContainer(c4d.ID_SHAPECONTAINER)  # Get copy of shape container
            if bsc is not None:
                bcd = bsc.GetContainer(c4d.ID_OPERATORCONTAINER)  # Get copy of operator container
                if bcd is not None:
                    # 位置
                    nodeInfo.px = bcd.GetReal(100)  # Get x position
                    nodeInfo.py = bcd.GetReal(101)  # Get y position

                    # 大小：XGroup特殊处理
                    if nodeInfo.node.IsGroupNode():
                        nodeInfo.sx = bcd.GetReal(110)  # Get XGroup x scale
                        nodeInfo.sy = bcd.GetReal(111)  # Get XGroup y scale
                        nodeInfo.pxGroup = bcd.GetReal(102) # xgroup inner xpos
                        nodeInfo.pyGroup = bcd.GetReal(103)  # xgroup inner ypos
                        nodeInfo.zoomGroup = bcd.GetReal(104)  # xgroup inner zoom
                        # print("nodeInfo:{},{} - {}".format(nodeInfo.pxGroup, nodeInfo.pyGroup, nodeInfo.zoomGroup))
                    else:
                        nodeInfo.sx = bcd.GetReal(108)  # Get x scale
                        nodeInfo.sy = bcd.GetReal(109)  # Get y scale

                    # print("{}CalcPosSize:{} - [{},{}] @ [{},{}]".format(Util.GetLeadingString(1), nodeInfo.node[c4d.ID_BASELIST_NAME],
                    #                                                     nodeInfo.px, nodeInfo.py, nodeInfo.sx, nodeInfo.sy))
                    return True

        return False

    def GetRealPortIDForAddPort(self, nodeInfoTarget, portSource):
        portSourceMainID = portSource.GetMainID()

        # port固有属性范围：[0, c4d_ID_OBJECT_PROPERTY_IN_FIRST)
        portSourceID = portSourceMainID

        # 以DescID的方法判断，仅适用于Object node——限定为Object node时，可省去1/3的时间，因为不需要复杂的正则表达式判断。
        # port对象属性/UserData范围：[c4d_ID_OBJECT_PROPERTY_IN_FIRST, c4d_ID_OBJECT_PROPERTY_SPEC_IN_FIRST)
        # 要先查找到正确的DescID，因为DescID和port.MainID是不同的
        if nodeInfoTarget.operatorID == c4d.ID_OPERATOR_OBJECT:
            if portSourceMainID in xrange(c4d_ID_OBJECT_PROPERTY_IN_FIRST, c4d_ID_OBJECT_PROPERTY_SPEC_IN_FIRST):
                portSourceID = self.GetPortDescID(portSource)

        return portSourceID
        
    """
    portID：即descID，参见E:\Program Files\Maxon Cinema 4D R21\resource\modules\expressiontag\description\gvobject.res
    portSourceDescID = c4d.DescID(c4d.DescLevel(portSourceMainID), c4d.DescLevel(portSourceSubID))
    """
    def AddPortByID(self, nodeInfoTarget, portIO, portID):
        print("{}PortID:{}".format(Util.GetLeadingString(1), portID))
        try:
            # if self.node.AddPortIsOK(portIO, portID):
            port = nodeInfoTarget.node.AddPort(portIO, portID)
            return port
        except Exception, err:
            print("[ERROR][2]{}".format(err))
            return None

    """
    普通port，按MainID和对应的次序(SubID为Key)匹配，否则，按None处理。
    """
    def GetPortByID(self, nodeInfoTarget, portSource):
        if portSource is None: return None

        printFlag = False

        portSourceMainID = portSource.GetMainID()
        portSourceSubID = portSource.GetSubID()

        if nodeInfoTarget.operatorID == c4d.ID_OPERATOR_OBJECT:
            # 不处理，视为没有
            if portSourceMainID in xrange(c4d_ID_OBJECT_PROPERTY_IN_FIRST, c4d_ID_OBJECT_PROPERTY_SPEC_IN_FIRST):
                return None
        else:  # 普通的port
            nodeSource = portSource.GetNode()
            portSourceList = self.GetSortedIOPortList(nodeSource)
            indexSource = -1
            for portSourceTemp in portSourceList:
                # 按MainID统计当前port所在的index
                if portSourceTemp.GetMainID() == portSourceMainID:
                    indexSource += 1
                # if portSourceTemp == portSource: # [ERROR]portSource对象不相等，原因不明。
                # TODO 使用SubID作为替代的判断
                if portSourceTemp.GetSubID() == portSourceSubID:
                    break

                if printFlag:
                    print("{}GetPortByIDS:[{},{}] - {}".format(Util.GetLeadingString(1), portSourceMainID,
                                                               portSourceSubID,
                                                               indexSource))

            portTargetList = self.GetSortedIOPortList(nodeInfoTarget.node)

            indexTarget = -1
            for portTarget in portTargetList:
                portTargetMainID = portTarget.GetMainID()
                portTargetSubID = portTarget.GetSubID()

                if printFlag:
                    print("Index:{} -- {} -- {} -- {}".format(indexTarget, indexSource, portTargetMainID,
                                                              portSourceMainID))
                if portTargetMainID == portSourceMainID:
                    indexTarget += 1
                    if indexTarget == indexSource:
                        return portTarget

                if printFlag:
                    print("{}GetPortByIDT:[{},{}] - {}".format(Util.GetLeadingString(1), portTargetMainID,
                                                               portTargetSubID,
                                                               indexTarget))

        return None
        
    """
    修正SetItemsValuesFrom里由于SubID的不同导致的值错误
    :param paramid 属于nodeTarget，即self.node
    """
    def ConvertPortSubIDList(self, nodeInfoTarget, paramid):
        # nodeSource = nodeInfoSource.node
        # portSource = None
        nodeTarget = nodeInfoTarget.node
        # portTarget = None
        portTargetSubIDList = []
        if paramid.GetDepth() == 2:
            try:
                portTargetList = self.GetSortedIOPortList(nodeTarget)
                for portTargetTemp in portTargetList:
                    # 按MainID统计当前port所在的index
                    if portTargetTemp.GetMainID() == paramid[0].id:
                        portTargetSubIDList.append(portTargetTemp.GetSubID())
            except Exception, err:
                print("[ERROR][11]{}".format(err))

        return portTargetSubIDList
        
    def AddNodePortsFrom(self, nodeInfoSource, nodeInfoTarget):
        if nodeInfoSource is None: return

        printFlag = True
        if printFlag: print("\n------------InPorts/OutPorts-----------")

        nodeSource = nodeInfoSource.node
        portSourceList = self.GetSortedIOPortList(nodeSource)
        nodeSourceDisplayName = nodeInfoSource.GetDisplayName()
        for portSource in portSourceList:
            portSourceName = portSource.GetName(nodeSource)
            portSourceMainID = portSource.GetMainID()
            portSourceSubID = portSource.GetSubID()
            # portSourceValue = nodeSource[portSourceMainID, portSourceSubID] # 访问错误
            portSourceValueType = portSource.GetValueType()
            portSourceIO = portSource.GetIO()
            portSourceIOString = dictIO[portSourceIO]
            if printFlag:
                print "S{}:{}.{} - [{},{}] - {}".format(portSourceIOString, nodeSourceDisplayName, portSourceName,
                                                        portSourceMainID, portSourceSubID, portSourceValueType)

            # 查找自己有没有对应的port，没有则添加
            portTarget = self.GetPortByID(nodeInfoTarget, portSource)
            if portTarget is None:
                # if printFlag: print("{}[None]:{}".format(Util.GetLeadingString(1), portSourceName))

                portSourceID = self.GetRealPortIDForAddPort(nodeInfoTarget, portSource)
                if portSourceID is None:
                    if printFlag:
                        print("{}ERROR[DescID]: {}.{}").format(indentString, nodeSourceDisplayName, portSourceName)
                    continue

                # 添加port：portSubID是要达到的SubID，如果没有达到，则循环添加至达到为止。
                portTarget = self.AddPortByID(nodeInfoTarget, portSourceIO, portSourceID)

                if portTarget is None:
                    if printFlag:
                        print("{}ERROR[AddPort]: {}.{}").format(indentString, nodeSourceDisplayName, portSourceName)
                    continue  # add ports ERROR

            portTarget.SetName(portSourceName)

            nodeTargetDisplayName = nodeInfoTarget.GetDisplayName()
            portTargetName = portTarget.GetName(nodeInfoTarget.node)
            portTargetMainID = portTarget.GetMainID()
            portTargetSubID = portTarget.GetSubID()

            if printFlag:
                print "{}T: {}.{} - [{},{}]".format(Util.GetLeadingString(1), nodeTargetDisplayName, portTargetName,
                                                    portTargetMainID, portTargetSubID)

            # 添加到dict，方便连接
            self.dictPortMapping[len(self.dictPortMapping)] = (portSource, portTarget)
            Util.NewLine(printFlag)

        Util.NewLine(printFlag)
        return

    """
        全面匹配，自动添加参数属性值，如Constant.Value, Constant.DataType，即可以直接[DescID]访问的参数值
        注意：处理的是如Math.input等的节点值，主要问题是由于SubID不一样而引起的input值不一样。
        而不是所引用对象的值。所以，Position、UserData等，无需要处理。
        无法访问以下参数，忽略处理，不影响：
            Spline.Object 2000
            Object.Object 30000003
            Object Index.Instance 2000
            Point.Object 2000
            Polygon.Object 2000
        """
    def SetItemsValuesFrom(self, nodeInfoSource, nodeInfoTarget):
        description = nodeInfoTarget.node.GetDescription(c4d.DESCFLAGS_DESC_0)
        for bc, paramid, groupid in description:
            stringInfoText = "{}DESC_NAME:{}({})".format(Util.GetLeadingString(1), bc[c4d.DESC_NAME], bc[c4d.DESC_IDENT])
            print(stringInfoText)
            #self.UpdateInfoText(stringInfoText) # 通过线程，在窗体显示信息DESC

            print("{}paramid:{} @ {}".format(Util.GetLeadingString(2), paramid, paramid[0].id))
            try:
                # paramid[0].id时，相当于[id]，即[MainID]，重复的项作为BaseContainer，即一次赋值多个同MainID的port的数值
                # paramid时，对普通项也适用，但对于重复的项，不适用。
                # paramid是DescID，paramid[0].id是DescLevel，即key
                id = paramid[0].id
                print("{}NodeInfoSource:{}".format(Util.GetLeadingString(2), nodeInfoSource[id]))
                # 只特殊处理同MainID而不同SubID的情况。
                if paramid.GetDepth() == 2:
                    # 如果是重复的项，则视为BaseContainer，并且key为SubID，按顺序把值一一对应赋予
                    # 注意：由于key不一样（SubID不一样），所以不能直接clone
                    if nodeInfoSource[id] is not None and type(nodeInfoSource[id]).__name__ == "BaseContainer":
                        # None表示没有赋值，即第一次赋值。如果不是，则可以跳过。
                        if nodeInfoTarget[id] is None:
                            values = []
                            for index, value in nodeInfoSource[id]:
                                print("{}values[{}] = {}".format(Util.GetLeadingString(3), index, value))
                                values.append(value)

                            portTargetSubIDList = self.ConvertPortSubIDList(nodeInfoTarget, paramid)
                            print("{}portTargetSubIDList:{}".format(Util.GetLeadingString(2), portTargetSubIDList))

                            valuesTarget = c4d.BaseContainer()
                            for index, value in enumerate(values):
                                portTargetSubID = portTargetSubIDList[index]
                                valuesTarget[portTargetSubID] = values[index]

                            for index, value in valuesTarget:
                                print("{}valuesTarget[{}] = {}".format(Util.GetLeadingString(2), index, value))

                            # 赋值(BaseContainer不能一个个值单独赋值，只能整体作为BaseContainer赋值)
                            nodeInfoTarget[id] = valuesTarget
                        else:
                            pass  # 同一MainID第二次遇到，不需要赋值。
                    else:
                        pass  # Source的值为None，不处理
                else:  # paramid.GetDepth() == 1
                    nodeInfoTarget[id] = nodeInfoSource[id]  # 普通
            except Exception, err:
                print "[ERROR][1]{}[{}] - {}".format(nodeInfoTarget.GetDisplayName(), paramid[0].id, err)
                continue
    
    """
    Factory工厂
    """
    def CreateNodeInfo(self, node):
        if node is None: return None

        # TODO 按operatorID添加其它类
        operatorID = node.GetOperatorID()
        # Object node
        if operatorID == c4d.ID_OPERATOR_OBJECT:
            return ObjectNodeInfo(node)

        if operatorID == c4d.ID_OPERATOR_MATH:
            return MathNodeInfo(node)
        if operatorID == c4d.ID_OPERATOR_FLOATMATH:
            return FloatMathNodeInfo(node)

        return NodeInfo(node)

    """
    按层级读取全部的node，包括node的operatorID并设置为对应的NodeInfo类，和位置、大小属性，最后全部保存为一个nodeInfo并返回。
    """
    def Read(self, node, hi):
        if node is None: return None

        nodeInfo = self.CreateNodeInfo(node)

        # 读取位置、大小属性
        self.CalcPosSize(nodeInfo)

        # 最后的child，本来应该使用nodeInfo.GetChildren()，而且本句本来应该放在for之后，
        # 但考虑到输出时，由于递归，nodeInfo.GetChildren()要先计算，如果放在最后面，会导致group和child的层级关系不明朗，
        # 因此，使用node.GetChildren()代替nodeInfo.GetChildren()作为计数。
        stringInfoText = "{}{}(oID: {}, child: {})".format(Util.GetLeadingString(hi), nodeInfo[c4d.ID_BASELIST_NAME],
                                                           nodeInfo.operatorID, len(node.GetChildren()))
        self.UpdateInfoText(stringInfoText)  # 通过线程，在窗体显示信息Read

        if self._workThread.TestBreak(): return None  # 如果已中断，马上退出。
        # 递归
        index = 1
        for nodeChild in node.GetChildren():
            if self._workThread.TestBreak(): return None  # 如果已中断，马上退出。

            if (hi == 1) or (hi == 0):
                stringInfoText = "Read:{}/{} - {}".format(index, len(node.GetChildren()), nodeChild[c4d.ID_BASELIST_NAME])
                self.UpdateInfoText(stringInfoText)  # 通过线程，在窗体显示信息Read/

            nodeInfoChild = self.Read(nodeChild, hi + 1)
            if nodeInfoChild is not None:
                nodeInfo.GetChildren().append(nodeInfoChild)

            index += 1

        return nodeInfo

    """
    只适用于范围内的，即在Object Properties里有的。
    """
    def GetPortDescID(self, port):
        printFlag = False
        try:
            node = port.GetNode()
            portName = port.GetName(port.GetNode())

            obj = node[c4d.GV_OBJECT_OBJECT_ID]
            if printFlag: print("-Port-:{}.{}".format(obj[c4d.ID_BASELIST_NAME], portName))
            if obj is not None:
                description = obj.GetDescription(c4d.DESCFLAGS_DESC_0)
                for bc, paramid, groupid in description:
                    descName = bc[c4d.DESC_NAME]
                    # print("DESCNAME:{}".format(descName))
                    # 分组（包括显式/隐式），都是没有DESC_NAME的
                    if descName is None or descName == "":
                        continue

                    # 正则表达式测试网址（需要等待）：https://regex101.com/
                    # 特别注意：匹配时，要使用decode('utf-8')的文字；而使用print输出时，要使用encode('utf-8')的文字，或者原文字。
                    # TODO 重要：空字符串不能encode/decode
                    # 匹配之后，matchObj的任何对象，如果要作比较，要使用统一的编码。
                    # 中文字符范围：[\u4e00-\u9fa5]
                    # 匹配：Position, Position . Z, Size . Y, Data . Off, Data . V2
                    # 匹配：Hello.1.1, `~Hello. 1@!#$% ^&*()-_<>,={};:'""\|.V1
                    # 匹配：统一颜色（开·~关）￥……&…^*(（;；：'‘’“”""“”『〔【…《。‘“—〈〉？！、；
                    #PATTERN_PORT_NAME = ur'^(?P<Main>([\u4e00-\u9fa5]|[A-Za-z0-9_]|[\(\)\[\]\{\}\<\>\s\+\-\*\/=\\\,\.]|[\`~!@#%\^\$&\?;:\'\"|])+?)(\s?\.\s?(?P<Sub>[XYZHPB]|Off|V1|V2|V3))?$'
                    PATTERN_PORT_NAME = ur'^(?P<Main>([\u0000-\u9fa5]|[A-Za-z0-9_]|[\(\)\[\]\{\}\<\>\s\+\-\*\/=\\\,\.]|[\`~!@#%\^\$&\?;:\'\"|]|[（）￥；：？！])+?)(\s?\.\s?(?P<Sub>[XYZHPB]|Off|V1|V2|V3))?$'
                    matchObj = re.match(PATTERN_PORT_NAME, portName.decode('utf-8'), re.M | re.I)
                    # if printFlag: print "----sign5:", descName #, "match: ", matchObj
                    if matchObj is not None:
                        # if printFlag: print("{}{}<-->{}".format(Util.GetLeadingString(1), descName, portName))
                        portNameMain = matchObj.group('Main')  # 已decode('utf-8')
                        portNameSub = matchObj.group('Sub')  # 已decode('utf-8')

                        # 匹配成功，看是不是XYZ等的分量
                        # 注意：用decode('utf-8')，因为matchObj的任何对象，都是decode('utf-8')了的。
                        if descName.decode('utf-8') == portNameMain:
                            desc = None
                            # portNameSub is None，匹配没有分量的情况
                            if portNameSub is None or portNameSub == "":
                                print("{}Main:{}".format(Util.GetLeadingString(1), portNameMain.encode('utf-8')))
                                # desc = c4d.DescID(c4d.DescLevel(paramid[0].id, paramid[0].dtype, 0))
                                desc = paramid
                            else:  # 有分量
                                print("{}Main:{}, Sub:{}".format(Util.GetLeadingString(1), portNameMain.encode('utf-8'),
                                                                 portNameSub.encode('utf-8')))
                                dictVectorXYZ = {"X": c4d.VECTOR_X, "Y": c4d.VECTOR_Y, "Z": c4d.VECTOR_Z,
                                                 "H": c4d.VECTOR_X, "P": c4d.VECTOR_Y, "B": c4d.VECTOR_Z,
                                                 "Off": c4d.MATRIX_OFF, "V1": c4d.MATRIX_V1, "V2": c4d.MATRIX_V2,
                                                 "V3": c4d.MATRIX_V3}
                                if dictVectorXYZ.has_key(portNameSub):
                                    desc = c4d.DescID(c4d.DescLevel(paramid[0].id, paramid[0].dtype, 0),
                                                      c4d.DescLevel(dictVectorXYZ[portNameSub], 0, 0))
                                else:  # no key, error
                                    pass
                            if printFlag:
                                print indentString, "|---paramid:", paramid
                                print indentString, "|---desc:", desc, "=", obj[desc]
                            return desc

            return None
        except Exception, err:
            print("{}[ERROR][7]{}".format(Util.GetLeadingString(1), err))
            return None

    """
    ConnectPorts并不是直接连接portSource和portTarget，而是portSource和portTarget本身是一一对应的关系。
    通过一一对应的关系，可以按portSource.GetDestination()找到portTarget.GetDestination()，同时一一对应，从而真正的连接起来。
    即：
        portSource ==多个连接==> portSource.GetDestination()
            portSource ==一一映射==> portTarget
            portSource.GetDestination() ==一一映射==> portTarget.GetDestination()
        portTarget ==多个连接==> portTarget.GetDestination()
    """
    def ConnectPorts(self):
        printFlag = True

        # 连接
        for key, value in self.dictPortMapping.items():
            if self._workThread.TestBreak(): return  # 如果已中断，马上退出。

            portSource, portTarget = value[0], value[1]
            stringInfoText = "ConnectPorts:{}/{} - {}".format(key, len(self.dictPortMapping.items()),
                                                              portSource.GetName(portSource.GetNode()))
            self.UpdateInfoText(stringInfoText)  # 通过线程，在窗体显示信息

            nodeSource = portSource.GetNode()
            nodeSourceName = nodeSource[c4d.ID_BASELIST_NAME]
            portSourceName = portSource.GetName(portSource.GetNode())
            nodeTarget = portTarget.GetNode()
            nodeTargetName = nodeTarget[c4d.ID_BASELIST_NAME]
            portTargetName = portTarget.GetName(portTarget.GetNode())

            # 起始port：普通node.outport和XGroup.inport/outport可以作为connect的起始port
            signConnectSourceable = False
            if (not nodeSource.IsGroupNode()) and (portSource.GetIO() == c4d.GV_PORT_OUTPUT):
                signConnectSourceable = True
            if (nodeSource.IsGroupNode()):
                signConnectSourceable = True

            if signConnectSourceable: # 只用于节省时间，可以直接用True代替。
                if printFlag:
                    print("{}{}.{} ==> {}.{}".format(Util.GetLeadingString(0), nodeSourceName, portSourceName, nodeTargetName,
                                                     portTargetName))

                portSourceDestinationList = portSource.GetDestination()
                if portSourceDestinationList is not None and len(portSourceDestinationList) != 0:
                    for portSourceDestination in portSourceDestinationList:
                        try:
                            portTargetDestination = self.GetPortTargetDestination(portSourceDestination)
                            # 真正的连接操作
                            if portTargetDestination is not None:
                                if printFlag:
                                    stringInfoText = "{}TD:{}.{}".format(Util.GetLeadingString(1),
                                                              portTargetDestination.GetNode()[c4d.ID_BASELIST_NAME],
                                                              portTargetDestination.GetName(portTargetDestination.GetNode()))
                                    self.UpdateInfoText(stringInfoText)  # 通过线程，在窗体显示信息Connect
                                portTarget.Connect(portTargetDestination)

                                nodeSourceDestination = portSourceDestination.GetNode()
                                nodeSourceDestinationName = nodeSourceDestination[c4d.ID_BASELIST_NAME]
                                portSourceDestinationName = portSourceDestination.GetName(portSourceDestination.GetNode())
                                nodeTargetDestination = portTargetDestination.GetNode()
                                nodeTargetDestinationName = nodeTargetDestination[c4d.ID_BASELIST_NAME]
                                portTargetDestinationName = portTargetDestination.GetName(portTargetDestination.GetNode())
                                if printFlag:
                                    print("{}{}.{} ++> {}.{}".format(Util.GetLeadingString(0), nodeSourceDestinationName,
                                                                     portSourceDestinationName, nodeTargetDestinationName,
                                                                     portTargetDestinationName))
                        except Exception, err:
                            print("{}[ERROR][8]{}".format(Util.GetLeadingString(1), err))
                            continue

                Util.NewLine(printFlag)

    """
    Sort Key
    """
    def SortPortBySubID(self, port):
        return port.GetSubID()

    """
    Source Target通用
    """
    def GetSortedIOPortList(self, nodeTarget):
        portTargetList = nodeTarget.GetInPorts()
        portTargetList.extend(nodeTarget.GetOutPorts())
        portTargetList.sort(key=self.SortPortBySubID)

        return portTargetList

    """
    由于在Object发现，Source和Target的MainID也会不一样，所以，只能依赖于dictPortMapping，而不能在Source和Target之间单纯的用MainID比较    
    """
    def GetPortTargetDestination(self, portSourceDestination):
        nodeSourceDestination = portSourceDestination.GetNode()

        portSourceTempList = []
        portTargetTempList = []
        for key, value in self.dictPortMapping.items():
            portSourceTemp, portTargetTemp = value[0], value[1]
            if portSourceTemp.GetNode() == nodeSourceDestination and portSourceTemp.GetMainID() == portSourceDestination.GetMainID():
                portSourceTempList.append(portSourceTemp)
                portTargetTempList.append(portTargetTemp)

        portSourceTempList.sort(key=self.SortPortBySubID)
        portTargetTempList.sort(key=self.SortPortBySubID)

        for index, value in enumerate(portSourceTempList):
            if value.GetSubID() == portSourceDestination.GetSubID():
                portTargetDestination = portTargetTempList[index]
                return portTargetDestination

        return None

    def Write(self, nodeInfoSource, nodeInfoTarget, hi):
        if nodeInfoSource is None or nodeInfoTarget is None: return False

        # 添加到dict，方便后面的连接
        self.dictNodeInfoMapping[len(self.dictNodeInfoMapping)] = (nodeInfoSource, nodeInfoTarget)

        # 设置通用属性
        nodeInfoTarget[c4d.ID_BASELIST_NAME] = nodeInfoSource[c4d.ID_BASELIST_NAME]

        # 禁用python node，防止意外
        if nodeInfoTarget.operatorID == c4d_ID_OPERATOR_PYTHON:
            nodeInfoTarget[c4d.ID_GVBASE_ENABLE] = False

        if self._workThread.TestBreak(): return None  # 如果已中断，马上退出。
        # 设置位置、大小、ports等（nodeInfoSource在Read的时候已经读取过位置、大小）
        self.SetPosSize(nodeInfoSource, nodeInfoTarget)
        # Util.PrintAllDescriptions(nodeInfoSource.node)

        # 设置引用对象、Math加减法模式等
        nodeInfoTarget.SetSpecItems(nodeInfoSource)
        self.AddNodePortsFrom(nodeInfoSource, nodeInfoTarget)

        if self._workThread.TestBreak(): return None  # 如果已中断，马上退出。
        # 设置属性值
        self.SetItemsValuesFrom(nodeInfoSource, nodeInfoTarget)
        # Util.PrintAllDescriptions(nodeInfoSource[c4d.GV_OBJECT_OBJECT_ID]) # 本句仅用于测试，可删除

        if self._workThread.TestBreak(): return None  # 如果已中断，马上退出。
        # self._nodeInfoStream.GetNodeData(nodeInfoSource.node) # 测试专用，不需要

        # 递归
        nodeTargetMaster = nodeInfoTarget.node.GetNodeMaster()
        index = 1
        for nodeInfoSourceChild in nodeInfoSource.GetChildren():
            if self._workThread.TestBreak(): return None  # 如果已中断，马上退出。

            if (hi == 1) or (hi == 0):
                stringInfoText = "Write:{}/{} - {}".format(index, len(nodeInfoSource.GetChildren()),
                                                           nodeInfoSourceChild[c4d.ID_BASELIST_NAME])
                self.UpdateInfoText(stringInfoText)  # 通过线程，在窗体显示信息Write

            operatorID = nodeInfoSourceChild.operatorID
            # print "OID:", operatorID
            nodeTarget = nodeInfoTarget.node
            nodeTargetChild = nodeTargetMaster.CreateNode(nodeTarget, operatorID, None, 100, 200)
            if nodeTargetChild is not None:
                nodeInfoTargetChild = self.CreateNodeInfo(nodeTargetChild)
                result = self.Write(nodeInfoSourceChild, nodeInfoTargetChild, hi + 1)
                # print "result:", result
                if result == False:
                    return False
            else:
                return False

            index += 1

        return True

class SimpleUnlocker(object):
    def __init__(self, workThread):
        self._workThread = workThread
        self._nodeInfoStream = NodeInfoStream(self._workThread)

    def RebuildXPresso(self, xpressoSource, xpressoTarget):
        if xpressoSource is None or xpressoTarget is None: return

        if self._workThread.TestBreak(): return  # 如果已中断，马上退出。
        # 从Source读取node信息
        nodeSourceRoot = xpressoSource.GetNodeMaster().GetRoot()
        nodeInfoSource = self._nodeInfoStream.Read(nodeSourceRoot, 0)
        # print nodeInfo
        Util.NewLine()  # 空行，作为分隔

        xpressoTarget[c4d.ID_BASELIST_NAME] = xpressoSource[c4d.ID_BASELIST_NAME]
        # 禁用Target表达式，防止意外
        xpressoTarget[c4d.EXPRESSION_ENABLE] = False

        # 写入node信息到Target
        nodeTargetRoot = xpressoTarget.GetNodeMaster().GetRoot()
        # 清空现在nodes，防止干扰
        for child in nodeTargetRoot.GetChildren():
            child.Remove()

        if self._workThread.TestBreak(): return  # 如果已中断，马上退出。
        nodeInfoTarget = self._nodeInfoStream.CreateNodeInfo(nodeTargetRoot)
        result = self._nodeInfoStream.Write(nodeInfoSource, nodeInfoTarget, 0)

        if self._workThread.TestBreak(): return  # 如果已中断，马上退出。
        self._nodeInfoStream.ConnectPorts()

    def Unlock(self, objectSource, objectTarget):
        if objectSource is None or objectTarget is None: return "objectSource / objectTarget is None"

        # print "Object is:", objectSource[c4d.ID_BASELIST_NAME]
        objectTarget[c4d.ID_BASELIST_NAME] = objectSource[c4d.ID_BASELIST_NAME]

        tagTarget = None
        tagTargetIndex = 0
        for tagSource in objectSource.GetTags():
            # print "|---", tagSource[c4d.ID_BASELIST_NAME], tagSource.GetType()
            Util.UnHideObject(tagSource)
            # or: if type(s).__name__ == "XPressoTag":
            if tagSource.GetType() == c4d.Texpresso:
                tagSource[c4d.EXPRESSION_ENABLE] = False  # 禁用Xpresso

                tagTarget, tagTargetIndex = Util.GetOrCreateTag(objectTarget, c4d.Texpresso, tagTargetIndex)
                if tagTarget is not None:
                    tagTargetIndex += 1

                    # 在界面打开xpresso editor，使计算位置、大小等信息有效
                    c4d.modules.graphview.OpenDialog(0, tagTarget.GetNodeMaster())

                    tagTarget[c4d.ID_BASELIST_NAME] = tagSource[c4d.ID_BASELIST_NAME]
                    self.RebuildXPresso(tagSource, tagTarget)

        # 递归前的准备（Null对象）
        if len(objectSource.GetChildren()) != 0:
            objectTargetChild = objectTarget.GetDown()
            if objectTargetChild is None:
                objectTargetChild = c4d.BaseObject(c4d.Onull)
                objectTargetChild.InsertUnderLast(objectTarget)

            # 递归（Null对象）
            index = 0
            for objectSourceChild in objectSource.GetChildren():
                self.Unlock(objectSourceChild, objectTargetChild)

                index += 1
                if index == len(objectSource.GetChildren()):
                    break

                objectTargetChild = objectTargetChild.GetNext()
                if objectTargetChild is None:
                    objectTargetChild = c4d.BaseObject(c4d.Onull)
                    objectTargetChild.InsertUnderLast(objectTarget)

class SimpleUnlockerThread(c4d.threading.C4DThread):

    # <editor-fold desc="变量for: c4d.SpecialEventAdd()，用于跨线程调用。">
    # 变量名，短的用于P1，长的用于P2（对应P1）。调用时，p1/p2最好都赋值，否则会报错，虽然不影响运行。
    # 不要使用0作为值，否则会导致PyCObject_AsVoidPtr with non-C-object错误。
    MSG_THREAD_FINISHED         = 1
    MSG_THREAD_INFO             = 2
    # </editor-fold>

    """
    :param ownerDialog The dialog owns this thread
    :type ownerDialog SimpleUnlockerDialog
    :param doc The active document
    :type doc c4d.documents.BaseDocument
    :param objectSource The object to unlock
    :type objectSource c4d.BaseObject
    """
    def __init__(self, ownerDialog, doc, objectSource):
        self._ownerDialog = ownerDialog
        self._doc = doc
        self._objectSource = objectSource
        self._objectTarget = None
        self._workError = ""
        self._aborted = False
        self._infoText = ""

    # <editor-fold desc="内部属性">

    @property
    def ownerDialog(self):
        return self._ownerDialog
    
    @property
    def doc(self):
        return self._doc
    
    @property
    def objectSource(self):
        return self._objectSource

    @property
    def workError(self):
        return self._workError

    @workError.setter
    def workError(self, value):
        self._workError = value
    
    @property
    def objectTarget(self):
        return self._objectTarget
    
    @property
    def aborted(self):
        return self._aborted

    @aborted.setter
    def aborted(self, value):
        self._aborted = value

    @property
    def infoText(self):
        return self._infoText

    # </editor-fold>

    """
    未使用，保留——以代码生成objectUnlock
    """
    def GetObjectUnlockFromCreate(self):
        objectUnlock = self.doc.SearchObject("Unlocker")
        if objectUnlock is None:
            try:
                objectUnlock = c4d.BaseObject(c4d.Onull)
                objectUnlock[c4d.ID_BASELIST_NAME] = "Unlocker"
                self.doc.InsertObject(objectUnlock)

                # python tag，用于显示所有的隐藏对象/tag
                # 添加UserData
                bcUserData = c4d.GetCustomDataTypeDefault(c4d.DTYPE_BASELISTLINK)
                bcUserData[c4d.DESC_NAME] = "Object To Unlock"
                descUserData = objectUnlock.AddUserData(bcUserData)
                objectUnlock[descUserData] = self.objectSource
                print("objectTarget = {}".format(self.objectSource))
                print("objectUnlock[descUserData] = {}".format(objectUnlock[descUserData]))

                # 插入python tag
                tagPython = c4d.BaseTag(c4d.Tpython)
                objectUnlock.InsertTag(tagPython)

                # 设置python tag优先级
                priority = c4d.PriorityData()
                priority.SetPriorityValue(c4d.PRIORITYVALUE_MODE, c4d.CYCLE_GENERATORS)
                priority.SetPriorityValue(c4d.PRIORITYVALUE_PRIORITY, 499)
                tagPython[c4d.EXPRESSION_PRIORITY] = priority

                # 设置python tag代码
                tagPython[c4d.TPYTHON_CODE] = codePython

                # c4d.EventAdd()
            except Exception, err:
                print("[ERROR]{}".format(err))
                return None

        return objectUnlock

    def GetObjectUnlockFromFile(self):
        objectUnlock = self.doc.SearchObject("Unlocker")
        if objectUnlock is None:
            try:
                dir, file = os.path.split(__file__)
                fn = os.path.join(dir, "Unlocker.c4d")
                #print("fn = " + fn)
                thread = c4d.threading.GeGetCurrentThread()
                if c4d.documents.MergeDocument(self.doc, fn, c4d.SCENEFILTER_OBJECTS, thread):
                    # 合并文档之后，重新查找
                    objectUnlock = self.doc.SearchObject("Unlocker")
                    #print("object = {}".format(objectUnlock))
            except Exception, err:
                print("[ERROR]{}".format(err))
                return None

        return objectUnlock

    """
    在正式调用线程前，做一点准备工作
    Begin -> Start -系统自动调用-> Main
    """
    def Begin(self):
        c4d.CallCommand(13957)  # Clear Console

        # Unlocker对象：先尝试从文档加载，如果不可以，则生成默认的版本。
        objectUnlock = self.GetObjectUnlockFromFile()
        if objectUnlock is None:
            objectUnlock = self.GetObjectUnlockFromCreate()

        # Rebuild Xpresso 有则直接使用，无则新生成Null
        objectRebuildXpresso = objectUnlock.GetDown()
        if objectRebuildXpresso is None:
            objectRebuildXpresso = c4d.BaseObject(c4d.Onull)
            objectRebuildXpresso.InsertUnderLast(objectUnlock)

        objectRebuildXpresso[c4d.ID_BASELIST_NAME] = "Rebuild Xpresso"

        # objectTarget 新生成Null
        objectTarget = objectRebuildXpresso.GetDown()
        # 先清除已经存在的objectTarget，防止干扰
        if objectTarget is not None:
            objectTarget.Remove()
        objectTarget = c4d.BaseObject(c4d.Onull)
        objectTarget.InsertUnderLast(objectRebuildXpresso)

        self._objectTarget = objectTarget
        self._aborted = False
        # 自动调用self.Main()
        self.Start(c4d.THREADMODE_ASYNC, c4d.THREADPRIORITY_NORMAL)

        return True

    """
    self.Start()会自动调用self.Main()
    """
    def Main(self):
        # 真正的Unlock操作
        unlocker = SimpleUnlocker(self)
        self.workError = unlocker.Unlock(self.objectSource, self.objectTarget)

        # 关闭界面xpresso editor，可省略——一旦关闭，再次打开将会自动最大化。
        # c4d.modules.graphview.CloseDialog(0)
        # 临时，方便观看源
        # c4d.modules.graphview.OpenDialog(0, self.objectSource.GetTags()[0].GetNodeMaster())

        # 当线程结束时，发送CoreMessage给所有Dialog。
        # 在SimpleUnlockerDialog.CoreMessage()里响应对应消息
        c4d.SpecialEventAdd(PLUGIN_ID, SimpleUnlockerThread.MSG_THREAD_FINISHED)

        # 更新界面，要在这里调用。
        c4d.EventAdd(c4d.EVENT_ENQUEUE_REDRAW)

    """
    作为是否中断的判断依据。如在已调用End()的情况下，应该有对应的变量使当前方法返回True，
    同时在线程的运行中，使用TestBreak()获取本方法返回值，以判断是否中断。
    aborted和End()：
        aborted：外部设置中断变量，在Command-Abort里设置
        TestBreak()：内部运行中的方法的中断判断依据。
        TestDBreak()：当调用TestBreak()时，系统自动使用TestDBreak()的结果。——该结果，依赖于aborted变量的值。
        End()：控制权返回到主线程，但运行中的方法，会继续执行。
        所以，如果想中断线程，要先设置aborted = True，再马上执行End(wait=False)。参见SimpleUnlockerDialog.Abort()
        
    流程：        
        SimpleUnlockerDialog.Command()
            检测：id == ID_BUTTON_ABORT
            如果True，执行SimpleUnlockerDialog.Abort()
        SimpleUnlockerDialog.Abort()
            设置：SimpleUnlockerThread.abroted = True
            调用：SimpleUnlockerThread.End(wait=False)，控制权即时返回。
        SimpleUnlockerThread.TestDBreak()
            检测：SimpleUnlockerThread.abroted == True，返回True
        线程运行方法中，凡可以中断的地方
            判断：SimpleUnlockerThread.TestBreak()。
                系统自动使用SimpleUnlockerThread.TestDBreak()的结果作为该结果。
            如果为True，则马上中断。
    """
    def TestDBreak(self):
        if self._aborted == True:
            return True

        return False

    """
    线程中无法直接响应窗体消息，以及一切和窗体有关的设置。要使用c4d.SpecialEventAdd()来发送消息，并在窗体中使用CoreMessage()响应。
    """
    def UpdateInfoText(self, infoText=""):
        print("InfoText:" + infoText)
        self._infoText = infoText
        c4d.SpecialEventAdd(PLUGIN_ID, SimpleUnlockerThread.MSG_THREAD_INFO) # 发送消息CoreMessage
        # self._ownerDialog.UpdateInfoText(infoText) # 本句无效
        # self._ownerDialog.SetString(SimpleUnlockerDialog.ID_STATICTEXT_INFO, infoText) # 本句无效

"""
参见：[sdk]py-texture_baker_r18
https://github.com/PluginCafe/cinema4d_py_sdk_extended/blob/43d59d2f4fa9b908d642cad0a2d8808a9b01b22d/plugins/py-texture_baker_r18/py-texture_baker_r18.pyp
"""
class SimpleUnlockerDialog(c4d.gui.GeDialog):

    # <editor-fold desc="Dialog IDs">
    ID_STATICTEXT_UNLOCKOBJECT          = 1000
    ID_LINK_OBJECTTOUNLOCK                = 1001

    ID_STATICTEXT_INFO                  = 1010

    ID_BUTTON_WORK                      = 1020
    ID_BUTTON_ABORT                     = 1021
    # </editor-fold>

    def __init__(self):
        self._staticTextInfo = None
        #self._isWorking = False
        self._aborted = False
        self._workThread = None

    def CreateLayout(self):
        title = "SimpleUnlock"
        self.SetTitle(title)

        # 不使用菜单栏
        self.AddGadget(c4d.DIALOG_NOMENUBAR, 0)

        # Link: Unlock Object
        if self.GroupBegin(id=0, flags=c4d.BFH_SCALEFIT, cols=2, rows=1, title="", groupflags=c4d.BORDER_GROUP_IN):
            # StaticText使用BFV_TOP，才能在垂直方向上勉强对齐。
            self._staticTextUnlockObject = self.AddStaticText(id=self.ID_STATICTEXT_UNLOCKOBJECT, initw=0, inith=0,
                                                              name="Object To Unlock ", borderstyle=0,
                                                              flags=c4d.BFH_LEFT | c4d.BFV_TOP)

            bcLinkUnlockObject = c4d.BaseContainer()
            bcLinkUnlockObject[c4d.LINKBOX_HIDE_ICON] = False
            bcLinkUnlockObject[c4d.LINKBOX_LAYERMODE] = False
            # TODO ? name无法显示名称（用前面加StaticText替代）
            self._linkObjectToUnlock = self.AddCustomGui(id=self.ID_LINK_OBJECTTOUNLOCK, pluginid=c4d.CUSTOMGUI_LINKBOX,
                                                       name="Object 2 Unlock", flags=c4d.BFH_SCALEFIT | c4d.BFV_CENTER,
                                                       minw=300, minh=30, customdata=bcLinkUnlockObject)
        self.GroupEnd()

        # Info
        if self.GroupBegin(id=0, flags=c4d.BFH_SCALEFIT, cols=1, rows=1, title="", groupflags=c4d.BORDER_GROUP_IN):
            self._staticTextInfo = self.AddStaticText(id=self.ID_STATICTEXT_INFO, initw=0, inith=0, name="Info Text", borderstyle=0,
                                                     flags=c4d.BFH_SCALEFIT | c4d.BFV_TOP)
        self.GroupEnd()

        # Buttons
        if self.GroupBegin(id=0, flags=c4d.BFH_CENTER, cols=2, rows=1, title="", groupflags=c4d.BORDER_GROUP_IN):
            self.AddButton(id=self.ID_BUTTON_WORK, flags=c4d.BFH_LEFT, initw=100, inith=15, name="Work")
            self.AddButton(id=self.ID_BUTTON_ABORT, flags=c4d.BFH_LEFT, initw=100, inith=15, name="Abort")
        self.GroupEnd()

        # 按钮的enable状态
        self.EnableButtons(False)

        return True

    """
    Button调用——开始
    """
    def Work(self):
        if self._workThread is not None and self._workThread.IsRunning():
            return

        # Retrieves selected document
        doc = c4d.documents.GetActiveDocument()
        if doc is None:
            return

        objectToUnlock = self._linkObjectToUnlock.GetData() # objectSource
        if objectToUnlock is None:
            c4d.gui.MessageDialog("objectToUnlock is None")
            return

        # 最好先停止所有的（c4d内部）Threads，以免发生冲突。
        c4d.StopAllThreads()

        # Initializes and start texture baker thread
        self._aborted = False
        self._workThread = SimpleUnlockerThread(self, doc, objectToUnlock)

        #stringInfoText = "objectToUnlock = {}".format(objectToUnlock)
        #self.UpdateInfoText(stringInfoText)  # 通过线程，在窗体显示信息

        # Initializes the thread
        if not self._workThread.Begin():
            textError = "[ERROR]{}".format(self._workThread.workError)
            print(textError)
            self.UpdateInfoText(textError) # 线程启动失败
            self.EnableButtons(False)
            return

        # 进程已启动，更新按钮状态。
        self.EnableButtons(True)

    def EnableButtons(self, working):
        self.Enable(self.ID_BUTTON_WORK, not working)
        self.Enable(self.ID_BUTTON_ABORT, working)

    """
    Button调用
    """
    def Abort(self):
        # Checks if there is a baking process currently
        if self._workThread is not None and self._workThread.IsRunning():
            self._aborted = True
            self._workThread.aborted = True
            self._workThread.End(wait=False)
            self._workThread = None

            self.EnableButtons(False)

    """
    id(即) = 1020(ID_BUTTON_WORK)
        msg.GetId() = 1648444244(c4d.BFM_ACTION)
        MSG[1835362660(c4d.BFM_ACTION_ID)] = 1020(ID_BUTTON_WORK)
        MSG[1835365985(c4d.BFM_ACTION_VALUE)] = 1
        MSG[1768976737] = 0
    模拟点击，参见：http://www.c4d.cn/forum.php?mod=viewthread&tid=20515
    :return True表示没有问题，False表示出错了。
    """
    def Command(self, id, msg):
        if id == self.ID_BUTTON_WORK:
            self.Work()
        elif id == self.ID_BUTTON_ABORT:
            self.Abort()

        return True

    def MesssageAsVoidPtr(self, msg):
        # 转换PyCObject为python数据，这里是int
        def PrivatePyCObjectAsVoidPtr(msgID):
            try:
                msgThreadP1 = msg.GetVoid(msgID)
                pythonapi.PyCObject_AsVoidPtr.restype = c_void_p
                pythonapi.PyCObject_AsVoidPtr.argtypes = [py_object]
                msgThreadP1_Ptr = pythonapi.PyCObject_AsVoidPtr(msgThreadP1)
                return msgThreadP1_Ptr
            except Exception, err:
                if msgID == c4d.BFM_CORE_PAR2:
                    pass # 使用c4d.SpecialEventAdd()时，p2参数不设置，默认为0，不报错
                else:
                    print("[ERROR]{}".format(err))

            return -1

        p1 = PrivatePyCObjectAsVoidPtr(c4d.BFM_CORE_PAR1)
        p2 = PrivatePyCObjectAsVoidPtr(c4d.BFM_CORE_PAR2)
        return p1, p2

    """
    响应Thread.Main()里调用的c4d.SpecialEventAdd(PLUGIN_ID)的消息。
        窗体可以获取thread的参数，thread调用窗体的参数、方法等无效。
        参见：http://www.plugincafe.com/forum/forum_posts.asp?TID=13272&OB=DESC
        注意：c4d.SpecialEventAdd()的时候，p1/p2都不要使用0作为值，否则会导致PyCObject_AsVoidPtr with non-C-object错误。
              已在转换过程里对p2作特殊处理，不报错。
    Thread.Main()
        只发生于调用：c4d.SpecialEventAdd(PLUGIN_ID)
            系统调用：CoreMessage() - MSG[1298360649] = 1000004(PLUGIN_ID)
    This Method is called automatically when Core (Main) Message is received.
    :param id: The ID of the gadget that triggered the event.
    :type id: int
    :param msg: The original message container
    :type msg: c4d.BaseContainer
    :return: False if there was an error, otherwise True.
    """
    def CoreMessage(self, id, msg):
        # print("MSG=======", id)
        # for key, value in msg:
        #     print("|===MSG[{}] = {}".format(key, value))

        if id == PLUGIN_ID:
            msgThreadP1, msgThreadP2 = self.MesssageAsVoidPtr(msg)
            # print("Info from thread x:[{},{}]".format(msgThreadP1, msgThreadP2))
            if msgThreadP1 == SimpleUnlockerThread.MSG_THREAD_FINISHED:
                # Sets Button enable states so only work button can be pressed
                self.EnableButtons(False)

                # If not aborted, means the baking finished
                if not self._aborted:
                    self.UpdateInfoText("Working Finished")

                    # 释放线程内存
                    self._workThread = None
                    return True
                else: # aborted
                    self.UpdateInfoText("Working Aborted")
                    return True
            if msgThreadP1 == SimpleUnlockerThread.MSG_THREAD_INFO:
                # 接收到在Thread里使用c4d.SpecialEventAdd()发出的消息。
                print("Info from thread:[{},{}]".format(msgThreadP1, msgThreadP2))
                print("{}Text = {}".format(Util.GetLeadingString(1), self._workThread.infoText))
                self.UpdateInfoText(self._workThread.infoText) # 窗体接收到线程发送的消息，更新信息。

            return True

        # 其它消息，使用系统默认处理
        return c4d.gui.GeDialog.CoreMessage(self, id, msg)

    """
    点击之后，有如下消息，可以判断，消息先经过Message()，再分发到Command()
        MSG[1835362660] = 1020(ID_BUTTON_WORK)
        MSG[1835365985] = 1
        MSG[1768976737] = 0
    Message机制，参见：GUI and Interaction Messages Manual : Cinema 4D C++ SDK  
        file:///E:/Projects/C4D/_Resources/CINEMA4DR21115SDKHTML20191203/html/page_manual_guimessages.html#page_manual_guimessages_messages_gadgetinteraction
        
    SendMessage示例（仅用于窗体中，用于thread中无效）：
        msg = c4d.BaseContainer(c4d.BFM_ACTION)
        msg[c4d.BFM_ACTION_VALUE] = "Text"
        msg = c4d.BaseContainer()
        self.SendMessage(self.ID_STATICTEXT_INFO, msg)
    """
    def Message(self, msg, result):
        # print("MSG----------" + str(msg.GetId()))
        # for key, value in msg:
        #     print("|---MSG[{}] = {}".format(key, value))

        return c4d.gui.GeDialog.Message(self, msg, result)

    def AskClose(self):
        if self._workThread is not None and self._workThread.IsRunning:
            self._workThread.End(True)
            self._workThread = None
            return False # 强行关闭

        return False # 关闭

    """
    响应SetTimer()
    """
    def Timer(self, msg):
        pass

    def UpdateInfoText(self, infoText=""):
        # 设置显示的文字(第一参数可以是控件的ID，也可以是控件变量，如self.staticTextInfo)
        self.SetString(self.ID_STATICTEXT_INFO, infoText)

class SimpleUnlockerCommandData(c4d.plugins.CommandData):
    dialog = None

    def Execute(self, doc):
        # Creates the dialog if its not already exists
        if self.dialog is None:
            self.dialog = SimpleUnlockerDialog()

        # Opens the dialog
        return self.dialog.Open(dlgtype=c4d.DLG_TYPE_ASYNC, pluginid=PLUGIN_ID, defaultw=300, defaulth=100)

    def RestoreLayout(self, sec_ref):
        # Creates the dialog if its not already exists
        if self.dialog is None:
            self.dialog = SimpleUnlockerDialog()

        # Restores the layout
        return self.dialog.Restore(pluginid=PLUGIN_ID, secret=sec_ref)

def main():
    # print("SimpleUnlocker register...")
    bmp = bitmaps.BaseBitmap()
    dir, file = os.path.split(__file__)
    fn = os.path.join(dir, "res", "tsimpleunlocker.tif")
    bmp.InitWith(fn)
    plugins.RegisterCommandPlugin(id=PLUGIN_ID, str="Simple Unlocker",
                                info=0, icon=bmp,
                                help="Simple Unlocker help",
                                dat=SimpleUnlockerCommandData())
    # print("SimpleUnlocker registered.")

if __name__ == '__main__':
    main()
