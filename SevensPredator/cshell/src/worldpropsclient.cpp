//------------------------------------------------------------------------------//
// 
// MODULE   : worldpropclnt.cpp
// 
// PURPOSE  : CWorldPropsClnt - Implementation
// 
// CREATED  : 02/14/2002
// 
// (c) 2002 LithTech, Inc.  All Rights Reserved
// 
//------------------------------------------------------------------------------//

//--Project Includes--

// Definition of this class
#include "worldpropsclient.h"

// Global pointers to client interfaces
#include "clientinterfaces.h"

//--Engine Includes--

// ILTClient definition
#include <iltclient.h>

//--Other includes--

// for Music et al
#include <iltsoundmgr.h>

// for sprintf
#include <stdio.h>

extern ILTTexInterface* g_pTexInterfaceLT;


//------------------------------------------------------------------------------
//	CWorldPropsClnt::CWorldPropsClnt()
//
//------------------------------------------------------------------------------
CWorldPropsClnt::CWorldPropsClnt()		
:
m_nFarZ(10000),
m_vBackgroundColor(1.0f,1.0f,1.0f),
m_bFogEnable(false),
m_vFogColor(1.0f,1.0f,1.0f),
m_nFogNearZ(0),
m_nFogFarZ(5000),
m_bSkyFogEnable(false),
m_nSkyFogNearZ(0),
m_nSkyFogFarZ(5000),
m_fSkyScale(1.0f)
{}


//------------------------------------------------------------------------------
//	CWorldPropsClnt::~CWorldPropsClnt()
//
//------------------------------------------------------------------------------
CWorldPropsClnt::~CWorldPropsClnt()		
{}


//------------------------------------------------------------------------------
//	CWorldPropsClnt::UnpackWorldProps(ILTMessage_Read *pMsgProps)
//
//------------------------------------------------------------------------------
void CWorldPropsClnt::UnpackWorldProps(ILTMessage_Read *pMsgProps)
{
	
	m_nFarZ = pMsgProps->Readuint32();
	m_vBackgroundColor = pMsgProps->ReadLTVector();
	m_bFogEnable = pMsgProps->Readbool();
	m_vFogColor = pMsgProps->ReadLTVector();
	m_nFogNearZ = pMsgProps->Readuint32();
	m_nFogFarZ = pMsgProps->Readuint32();
	m_bSkyFogEnable = pMsgProps->Readbool();
	m_nSkyFogNearZ = pMsgProps->Readuint32();
	m_nSkyFogFarZ = pMsgProps->Readuint32();
	m_fSkyScale = pMsgProps->Readfloat();
				
	this->ApplyWorldProps();
	
}


//------------------------------------------------------------------------------
//	CWorldPropsClnt::ApplyWorldProps()
//
//------------------------------------------------------------------------------
void CWorldPropsClnt::ApplyWorldProps()
{

    // Set worldprop console variables.
    char buffer[255];

    sprintf(buffer, "FarZ %d", m_nFarZ);
    g_pLTClient->RunConsoleString(buffer);
    //		g_pLTClient->CPrint(buffer);

    sprintf(buffer, "BackgroundColor %d %d %d", (uint8)m_vBackgroundColor.x,
        (uint8)m_vBackgroundColor.y, (uint8)m_vBackgroundColor.z);
    g_pLTClient->RunConsoleString(buffer);
    //		g_pLTClient->CPrint(buffer);

    sprintf(buffer, "FogEnable %d", m_bFogEnable ? 1 : 0);
    g_pLTClient->RunConsoleString(buffer);
    //		g_pLTClient->CPrint(buffer);

    if (m_bFogEnable)
    {
        // Set fog rgb colors
        sprintf(buffer, "FogR %d", (uint8)m_vFogColor.x);
        g_pLTClient->RunConsoleString(buffer);
        sprintf(buffer, "FogG %d", (uint8)m_vFogColor.y);
        g_pLTClient->RunConsoleString(buffer);
        sprintf(buffer, "FogB %d", (uint8)m_vFogColor.z);
        g_pLTClient->RunConsoleString(buffer);

        sprintf(buffer, "FogNearZ %d; FogFarZ %d",
            m_nFogNearZ, m_nFogFarZ);
        g_pLTClient->RunConsoleString(buffer);
        //			g_pLTClient->CPrint(buffer);
    }

    sprintf(buffer, "SkyFogEnable %d", m_bSkyFogEnable ? 1 : 0);
    g_pLTClient->RunConsoleString(buffer);
    //		g_pLTClient->CPrint(buffer);
    if (m_bSkyFogEnable)
    {
        sprintf(buffer, "SkyFogNearZ %d; SkyFogFarZ %d",
            m_nSkyFogNearZ, m_nSkyFogFarZ);
        g_pLTClient->RunConsoleString(buffer);
        //			g_pLTClient->CPrint(buffer);
    }

    sprintf(buffer, "SkyScale %f", m_fSkyScale);
    g_pLTClient->RunConsoleString(buffer);

}
//------------------------------------------------------------------------------
//	CWorldPropsClnt::Update()
//
//------------------------------------------------------------------------------
void CWorldPropsClnt::Update()
{
    // Set worldprop console variables.
    char buffer[255];
}

//------------------------------------------------------------------------------
//	CWorldPropsClnt::GetBackgroundColor()
//
//------------------------------------------------------------------------------
LTRGB CWorldPropsClnt::GetBackgroundColor()
{
	
	LTRGB rgbColor;
	HCONSOLEVAR hCVar;
	
	rgbColor.r = 0;
	rgbColor.g = 0;
	rgbColor.b = 0;
	
	if (hCVar = g_pLTClient->GetConsoleVar("BackgroundR"))
		rgbColor.r = (uint8)g_pLTClient->GetVarValueFloat(hCVar);
	
	if (hCVar = g_pLTClient->GetConsoleVar("BackgroundG"))
		rgbColor.g = (uint8)g_pLTClient->GetVarValueFloat(hCVar);
	
	if (hCVar = g_pLTClient->GetConsoleVar("BackgroundB"))
		rgbColor.b = (uint8)g_pLTClient->GetVarValueFloat(hCVar);
	
	rgbColor.a = 255;
	
	return rgbColor;
}

// CWorldPropsClnt.cpp
void CWorldPropsClnt::InitBackgroundMusic()
{
    if (!g_pLTCSoundMgr) return;

    PlaySoundInfo snd;
    PLAYSOUNDINFO_INIT(snd); // 1️⃣ MUST be first to zero-initialize the struct

    snd.m_dwFlags = PLAYSOUND_CLIENT | PLAYSOUND_LOOP;
    strncpy(snd.m_szSoundName, "Music/BGM1.wav", sizeof(snd.m_szSoundName) - 1);
    snd.m_szSoundName[sizeof(snd.m_szSoundName) - 1] = '\0'; // Ensure null termination

    //snd.m_fVolume = 1.0f;
    //snd.m_fPitch = 1.0f;

    HLTSOUND hSound = LTNULL;
    LTRESULT result = g_pLTCSoundMgr->PlaySound(&snd, hSound);
    if (result != LT_OK)
    {
        g_pLTClient->CPrint("Failed to play BGM: %s", snd.m_szSoundName);
    }
    else
    {
        g_pLTClient->CPrint("Playing background music: Music/BGM1.wav");
    }
}


