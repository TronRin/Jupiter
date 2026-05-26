project "LIB-DShow"
	kind "StaticLib"
	language "C++"
	cppdialect "C++20"
	staticruntime "on"
	warnings "Extra"

	pchheader "streams.h"
	pchsource "src/dllentry.cpp"

	callingconvention "StdCall"

	targetdir (bindir)
	objdir (intdir .. "/%{prj.name}")

	defines "FILTER_DLL"

	files
	{
		"src/**.h", "src/**.cpp", "src/**.c"
	}

	includedirs
	{
		"src"
	}

	-- DX9 library
	syslibdirs
	{
		vendir .. "/directx-9x/lib"
	}

	links
	{
		"strmiids"
	}

	buildoptions { "/Zc:strictStrings-" }