"""Build the unsigned Send to Matins shortcut; `shortcuts sign` signs it after."""
import plistlib
import sys
import uuid

OC="\ufffc"
address_uuid=str(uuid.uuid4()).upper()
def token(s_before,var,after=""):
    return {"Value":{"string":s_before+OC+after,"attachmentsByRange":{f"{{{len(s_before)},1}}":var}},"WFSerializationType":"WFTextTokenString"}
inp={"Type":"ExtensionInput"}
addr={"Type":"ActionOutput","OutputName":"Text","OutputUUID":address_uuid}
actions=[
 {"WFWorkflowActionIdentifier":"is.workflow.actions.gettext","WFWorkflowActionParameters":{"WFTextActionText":"","UUID":address_uuid}},
 {"WFWorkflowActionIdentifier":"is.workflow.actions.downloadurl","WFWorkflowActionParameters":{
   "WFURL":token("",addr,"save"),
   "WFHTTPMethod":"POST","ShowHeaders":False,"WFHTTPBodyType":"JSON",
   "WFJSONValues":{"Value":{"WFDictionaryFieldValueItems":[
     {"WFItemType":0,"WFKey":{"Value":{"string":"url","attachmentsByRange":{}},"WFSerializationType":"WFTextTokenString"},
      "WFValue":token("",inp)}]},"WFSerializationType":"WFDictionaryFieldValue"}}},
 {"WFWorkflowActionIdentifier":"is.workflow.actions.notification","WFWorkflowActionParameters":{"WFNotificationActionTitle":"Matins","WFNotificationActionBody":"Added to your next issue"}},
]
doc={"WFWorkflowName":"Send to Matins","WFWorkflowActions":actions,"WFWorkflowClientVersion":"2612.0.4","WFWorkflowMinimumClientVersion":900,"WFWorkflowMinimumClientVersionString":"900",
 "WFWorkflowHasShortcutInputVariables":True,"WFWorkflowInputContentItemClasses":["WFURLContentItem","WFSafariWebPageContentItem"],"WFWorkflowTypes":["ActionExtension"],
 "WFWorkflowIcon":{"WFWorkflowIconGlyphNumber":59511,"WFWorkflowIconStartColor":4274264319},
 "WFWorkflowImportQuestions":[{"ActionIndex":0,"Category":"Parameter","DefaultValue":"","ParameterKey":"WFTextActionText","Text":"Paste your Matins address (Settings → Send to Matins, it was copied for you)"}]}
plistlib.dump(doc,open(sys.argv[1],"wb"),fmt=plistlib.FMT_BINARY)
